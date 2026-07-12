import pytest
from unittest.mock import MagicMock, AsyncMock
from transitions.core import MachineError
from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine, BootStrategy, KernelPanicError, DutState


def _pin_cfg_defaults(mock_cfg: MagicMock) -> None:
    """Pin the six Fix-6 StateMachineConfig fields to their real default values.

    ``getattr(self.cfg, ..., default)`` call sites were converted to direct
    attribute access; on a bare ``MagicMock()`` cfg every attribute access
    auto-vivifies a truthy ``MagicMock`` instead of the documented default,
    which silently flips ``if`` branches gated on these fields. Pin them
    explicitly so FSM unit tests keep exercising the same code paths as
    before the Fix-6 config change.
    """
    mock_cfg.gpio_reset_pin = None
    mock_cfg.gpio_recovery_pin = "RECOVERY_BTN"
    mock_cfg.recovery_latch_time_s = 1.5
    mock_cfg.power_off_threshold_a = 0.05
    mock_cfg.boot_straps_gpio_map = {}
    mock_cfg.storage_data_encrypted = "/dev/mapper/data_crypt"

class MockBootStrategy(BootStrategy):
    async def cold_boot_to_bootloader(self, fsm: EmbeddedLinuxStateMachine) -> None:
        fsm.machine.set_state(DutState.BOOTLOADER)
        
    async def cold_boot_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
        fsm.machine.set_state(DutState.OS_USERLAND)
        
    async def resume_bootloader_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
        fsm.machine.set_state(DutState.OS_USERLAND)

@pytest.mark.anyio
async def test_fsm_power_cycle_error_handling(monkeypatch):
    """Edge Case: What happens if the PSU fails to turn on?"""
    monkeypatch.setattr('builtins.input', lambda _: None)
    serial_mock = MagicMock()
    serial_mock.raw_read_chunk.return_value = b""
    psu_mock = MagicMock()
    psu_mock.enable_output.side_effect = Exception("PSU Exploded")
    
    ssh_mock = MagicMock()
    ssh_mock.is_connected = False
    ssh_mock.ip_address = "127.0.0.1"
    ssh_mock.cfg.port = 2222

    mock_cfg = MagicMock()
    _pin_cfg_defaults(mock_cfg)

    fsm = EmbeddedLinuxStateMachine(
        psu=psu_mock,
        serial=serial_mock,
        ssh=ssh_mock,
        cfg=mock_cfg
    )
    fsm.boot_strategy = MockBootStrategy()

    # Try to energize (which will call _hw_energize -> _do_energize -> psu.power_on)
    with pytest.raises(Exception, match="PSU Exploded"):
        await fsm.energize()
        
@pytest.mark.anyio
async def test_fsm_panic_event_during_boot(monkeypatch):
    """Edge Case: UartEventStream yields a KernelPanic."""
    monkeypatch.setattr('builtins.input', lambda _: None)
    from pytest_mes_core.state_machine import UartEventStream, PanicDetected
    
    serial_mock = MagicMock()
    serial_mock.raw_read_chunk.return_value = b""
    stream_mock = MagicMock()
    
    # Simulate the stream yielding normal lines, then a panic
    import time
    async def mock_open_async(*args, **kwargs):
        yield PanicDetected(elapsed_s=time.time(), raw_output="Kernel panic - not syncing: VFS: Unable to mount root fs")
        
    stream_mock.open_async = mock_open_async
    
    class PanicBootStrategy(BootStrategy):
        async def cold_boot_to_bootloader(self, fsm: EmbeddedLinuxStateMachine) -> None:
            pass

        async def cold_boot_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
            # We must use the stream. If it yields PanicDetected, we should raise.
            async for event in stream_mock.open_async():
                if isinstance(event, PanicDetected):
                    raise KernelPanicError(event.raw_output)

        async def resume_bootloader_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
            pass
                    
    mock_cfg = MagicMock()
    mock_cfg.cold_boot_timeout_s = 60.0
    _pin_cfg_defaults(mock_cfg)
    fsm = EmbeddedLinuxStateMachine(
        psu=MagicMock(),
        serial=serial_mock,
        ssh=MagicMock(),
        cfg=mock_cfg
    )
    fsm.event_stream = stream_mock
    fsm.boot_strategy = PanicBootStrategy()
    
    # Energize the board first
    await fsm.energize()
    
    with pytest.raises(KernelPanicError, match="Device kernel panicked during OS boot sequence."):
        await fsm.boot_to_os()

@pytest.mark.anyio
async def test_fsm_timeout_during_wait_for_shell(monkeypatch):
    """Edge Case: The board hangs completely during boot, event stream times out."""
    monkeypatch.setattr('builtins.input', lambda _: None)
    from pytest_mes_core.transports import TransportTimeoutError
    
    serial_mock = MagicMock()
    stream_mock = MagicMock()
    
    # Mock open to yield nothing and just exit (simulating a timeout where the while loop ends)
    async def mock_open_async(*args, **kwargs):
        return
        yield
        
    stream_mock.open_async = mock_open_async
    
    mock_cfg = MagicMock()
    mock_cfg.cold_boot_timeout_s = 1.0
    _pin_cfg_defaults(mock_cfg)

    fsm = EmbeddedLinuxStateMachine(
        psu=MagicMock(),
        serial=serial_mock,
        ssh=MagicMock(),
        cfg=mock_cfg
    )
    fsm.event_stream = stream_mock
    fsm.boot_strategy = MockBootStrategy()

    with pytest.raises(TransportTimeoutError, match="Timed out waiting for Linux Shell prompt."):
        # We invoke the private method to specifically test the shell wait timeout
        await fsm.event_wait_for_os_shell()

@pytest.mark.anyio
async def test_fsm_hot_login_fallback_to_cold_boot(monkeypatch):
    """Edge Case: Hot login fails because the target doesn't show a shell prompt,
    so the FSM must fall back to a full cold boot power cycle."""
    monkeypatch.setattr('builtins.input', lambda _: None)
    from pytest_mes_core.transports import TransportTimeoutError

    serial_mock = MagicMock()
    stream_mock = MagicMock()

    # The first time we wait for shell (during hot login), it times out.
    # The second time (during cold boot), it succeeds.
    async def mock_open_fail(*args, **kwargs):
        return
        yield

    async def mock_open_success(*args, **kwargs):
        from pytest_mes_core.state_machine import PromptDetected
        import time
        yield PromptDetected(elapsed_s=time.time(), prompt_type="shell")

    stream_mock.open_async.side_effect = [mock_open_fail(), mock_open_success()]

    mock_cfg = MagicMock()
    mock_cfg.cold_boot_timeout_s = 1.0
    mock_cfg.os_user = "root"
    _pin_cfg_defaults(mock_cfg)

    from transitions.core import EventData
    mock_event = MagicMock(spec=EventData)
    mock_event.kwargs = {}

    psu_mock = MagicMock()
    # Ensure measure_current raises AttributeError so _do_energize is fast
    del psu_mock.measure_current

    ssh_mock = MagicMock()
    ssh_mock.is_connected = False
    ssh_mock.ip_address = "127.0.0.1"
    ssh_mock.cfg.port = 2222

    fsm = EmbeddedLinuxStateMachine(
        psu=psu_mock,
        serial=serial_mock,
        ssh=ssh_mock,
        cfg=mock_cfg
    )

    # Ensure raw_read_chunk returns empty bytes to prevent infinite loops in UART probe
    serial_mock.raw_read_chunk.return_value = b""
    fsm.event_stream = stream_mock

    class SpyBootStrategy(MockBootStrategy):
        """Records whether cold_boot_to_os is actually reached, and lets
        finalize_os_boot() short-circuit immediately (as it would once a real
        cold boot has bridged the SSH transport)."""
        def __init__(self):
            self.cold_boot_to_os_called = False

        async def cold_boot_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
            self.cold_boot_to_os_called = True
            fsm.machine.set_state(DutState.OS_USERLAND)
            ssh_mock.is_connected = True

    spy_strategy = SpyBootStrategy()
    fsm.boot_strategy = spy_strategy

    # Mock do_power_off and do_energize to not sleep for 3 seconds during tests
    fsm._do_power_off = MagicMock()
    fsm._do_energize = MagicMock()

    await fsm.energize()

    # Act: call _hw_boot_to_os directly since it handles the fallback logic
    await fsm._hw_boot_to_os(mock_event)

    # Assert that power cycle occurred because hot login failed, AND that the
    # escalation actually cascaded all the way into a real cold boot (Fix 3).
    fsm._do_power_off.assert_called()
    assert spy_strategy.cold_boot_to_os_called, "hot-login failure did not escalate into cold_boot_to_os"
    assert fsm.state == DutState.OS_USERLAND


@pytest.mark.anyio
async def test_fsm_os_userland_heartbeat_fail_escalates_to_hot_login(monkeypatch):
    """Edge Case: an existing OS_USERLAND session's heartbeat fails (board
    silently rebooted underneath us). The FSM must reset and attempt a
    hot-login next rather than returning with a stale OS_USERLAND state
    while the board is mid-reboot (Fix 3)."""
    monkeypatch.setattr('builtins.input', lambda _: None)
    from pytest_mes_core.transports import TransportConnectionError

    serial_mock = MagicMock()
    serial_mock.is_connected = False
    stream_mock = MagicMock()

    async def mock_open_success(*args, **kwargs):
        from pytest_mes_core.state_machine import PromptDetected
        import time
        yield PromptDetected(elapsed_s=time.time(), prompt_type="shell")

    stream_mock.open_async.side_effect = [mock_open_success()]

    mock_cfg = MagicMock()
    mock_cfg.cold_boot_timeout_s = 1.0
    _pin_cfg_defaults(mock_cfg)

    psu_mock = MagicMock()
    del psu_mock.measure_current

    ssh_mock = MagicMock()
    ssh_mock.is_connected = True
    ssh_mock.ip_address = "127.0.0.1"
    ssh_mock.cfg.port = 2222
    # Heartbeat over the existing SSH session fails -> board must be a zombie.
    ssh_mock.async_safe_run = AsyncMock(side_effect=TransportConnectionError("dead"))

    fsm = EmbeddedLinuxStateMachine(
        psu=psu_mock,
        serial=serial_mock,
        ssh=ssh_mock,
        cfg=mock_cfg,
    )
    fsm.event_stream = stream_mock
    fsm.machine.set_state(DutState.OS_USERLAND)

    fsm._do_power_off = MagicMock()
    fsm._do_energize = MagicMock()
    fsm.finalize_os_boot = AsyncMock()

    # Act: drive through the real trigger so a successful hot-login is
    # reflected by the transitions library's own state assignment.
    await fsm.boot_to_os()

    # Assert: the hot-login stream was opened (i.e. _try_hot_login ran, not a
    # silent return stuck at OS_USERLAND), and it succeeded.
    assert stream_mock.open_async.call_count == 1
    fsm.finalize_os_boot.assert_awaited()
    assert fsm.state == DutState.OS_USERLAND

@pytest.mark.anyio
async def test_fsm_verbose_logging_prints_at_info(monkeypatch):
    """Verify that when verbose=True, boot logs are emitted at INFO level, otherwise DEBUG."""
    import pytest_mes_core.state_machine
    from pytest_mes_core.events import BootDataReceived, PromptDetected
    
    mock_logger = MagicMock()
    monkeypatch.setattr(pytest_mes_core.state_machine, "logger", mock_logger)
    
    serial_mock = MagicMock()
    mock_res = MagicMock()
    mock_res.stdout = "MES_SYNC"
    serial_mock.safe_run.return_value = mock_res
    
    stream_mock = MagicMock()
    async def mock_open_async(*args, **kwargs):
        yield BootDataReceived(elapsed_s=1.0, line="Loading Linux kernel...")
        yield PromptDetected(elapsed_s=2.0, prompt_type="bootloader")
        
    stream_mock.open_async = mock_open_async
    
    mock_cfg = MagicMock()
    mock_cfg.cold_boot_timeout_s = 60.0
    _pin_cfg_defaults(mock_cfg)

    # 1. Test verbose=False (Default)
    fsm_quiet = EmbeddedLinuxStateMachine(
        psu=MagicMock(), serial=serial_mock, ssh=MagicMock(), cfg=mock_cfg, verbose=False
    )
    fsm_quiet.event_stream = stream_mock
    
    await fsm_quiet.event_wait_for_bootloader(intercept_autoboot=True)
        
    mock_logger.debug.assert_any_call("uart_rx", data="Loading Linux kernel...")
    mock_logger.reset_mock()
    
    # 2. Test verbose=True
    stream_mock.open_async = mock_open_async
    fsm_verbose = EmbeddedLinuxStateMachine(
        psu=MagicMock(), serial=serial_mock, ssh=MagicMock(), cfg=mock_cfg, verbose=True
    )
    fsm_verbose.event_stream = stream_mock
    
    await fsm_verbose.event_wait_for_bootloader(intercept_autoboot=True)

    mock_logger.info.assert_any_call("uart_rx", data="Loading Linux kernel...")


@pytest.mark.anyio
async def test_fsm_warm_reboot_uses_trap_when_autoboot_disabled(monkeypatch):
    """Fix 4: from OS_USERLAND with autoboot disabled, the FSM must use the
    U-Boot trap-and-reboot flow instead of a plain 'reboot' — with no trap
    installed a plain reboot sails straight past the hidden bootloader
    prompt and the wait guaranteed-times-out."""
    monkeypatch.setattr('builtins.input', lambda _: None)

    serial_mock = MagicMock()
    ssh_mock = MagicMock()
    ssh_mock.is_connected = False

    mock_cfg = MagicMock()
    mock_cfg.autoboot_enabled = False
    _pin_cfg_defaults(mock_cfg)

    fsm = EmbeddedLinuxStateMachine(
        psu=MagicMock(),
        serial=serial_mock,
        ssh=ssh_mock,
        cfg=mock_cfg,
    )
    fsm.machine.set_state(DutState.OS_USERLAND)

    fsm._set_uboot_trap_and_reboot = MagicMock()
    fsm.event_wait_for_bootloader = AsyncMock()

    from transitions.core import EventData
    mock_event = MagicMock(spec=EventData)
    mock_event.kwargs = {}

    await fsm._hw_boot_to_bootloader(mock_event)

    fsm._set_uboot_trap_and_reboot.assert_called_once()
    serial_mock.async_safe_run.assert_not_called()
    fsm.event_wait_for_bootloader.assert_awaited_once_with(intercept_autoboot=False)


@pytest.mark.anyio
async def test_fsm_state_changed_event_carries_state_names(monkeypatch):
    """Fix 1: StateChanged.new_state/old_state must be enum *names* (e.g.
    "ENERGIZED"), not the numeric auto() value pydantic would otherwise
    coerce an Enum member into (e.g. "2")."""
    monkeypatch.setattr('builtins.input', lambda _: None)
    from pytest_mes_core.events import bus, hookimpl, StateChanged

    captured = []

    class Listener:
        @hookimpl
        def on_state_changed(self, event: StateChanged) -> None:
            captured.append(event)

    listener = Listener()
    bus.register(listener)

    try:
        psu_mock = MagicMock()
        del psu_mock.measure_current

        serial_mock = MagicMock()
        serial_mock.raw_read_chunk.return_value = b""

        ssh_mock = MagicMock()
        ssh_mock.is_connected = False
        ssh_mock.ip_address = "127.0.0.1"
        ssh_mock.cfg.port = 2222

        mock_cfg = MagicMock()
        _pin_cfg_defaults(mock_cfg)

        fsm = EmbeddedLinuxStateMachine(
            psu=psu_mock,
            serial=serial_mock,
            ssh=ssh_mock,
            cfg=mock_cfg,
        )

        await fsm.energize()
    finally:
        bus.pm.unregister(listener)

    assert captured, "on_state_changed was never fired"
    last = captured[-1]
    assert last.new_state == "ENERGIZED"
    assert isinstance(last.new_state, str) and not last.new_state.isdigit()
    assert isinstance(last.old_state, str) and not last.old_state.isdigit()
