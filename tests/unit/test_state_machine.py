import pytest
from unittest.mock import MagicMock
from transitions.core import MachineError
from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine, BootStrategy, KernelPanicError, DutState

class MockBootStrategy(BootStrategy):
    def cold_boot_to_bootloader(self, fsm: EmbeddedLinuxStateMachine) -> None:
        fsm.machine.set_state(DutState.BOOTLOADER)
        
    def cold_boot_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
        fsm.machine.set_state(DutState.OS_USERLAND)
        
    def resume_bootloader_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
        fsm.machine.set_state(DutState.OS_USERLAND)

    async def async_cold_boot_to_bootloader(self, fsm: EmbeddedLinuxStateMachine) -> None:
        fsm.machine.set_state(DutState.BOOTLOADER)
        
    async def async_cold_boot_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
        fsm.machine.set_state(DutState.OS_USERLAND)
        
    async def async_resume_bootloader_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
        fsm.machine.set_state(DutState.OS_USERLAND)



def test_fsm_power_cycle_error_handling(monkeypatch):
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
    
    fsm = EmbeddedLinuxStateMachine(
        psu=psu_mock,
        serial=serial_mock,
        ssh=ssh_mock,
        cfg=MagicMock()
    )
    fsm.boot_strategy = MockBootStrategy()
    
    # Try to energize (which will call _hw_energize -> _do_energize -> psu.power_on)
    with pytest.raises(Exception, match="PSU Exploded"):
        fsm.energize()
        
def test_fsm_panic_event_during_boot(monkeypatch):
    """Edge Case: UartEventStream yields a KernelPanic."""
    monkeypatch.setattr('builtins.input', lambda _: None)
    from pytest_mes_core.state_machine import UartEventStream, PanicDetected
    
    serial_mock = MagicMock()
    serial_mock.raw_read_chunk.return_value = b""
    stream_mock = MagicMock()
    
    # Simulate the stream yielding normal lines, then a panic
    import time
    def mock_open(*args, **kwargs):
        yield PanicDetected(elapsed_s=time.time(), raw_output="Kernel panic - not syncing: VFS: Unable to mount root fs")
        
    stream_mock.open.side_effect = mock_open
    
    class PanicBootStrategy(BootStrategy):
        def cold_boot_to_bootloader(self, fsm: EmbeddedLinuxStateMachine) -> None:
            pass
            
        def cold_boot_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
            # We must use the stream. If it yields PanicDetected, we should raise.
            for event in stream_mock.open():
                if isinstance(event, PanicDetected):
                    raise KernelPanicError(event.raw_output)
                    
        def resume_bootloader_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
            pass
                    
        async def async_cold_boot_to_bootloader(self, fsm: EmbeddedLinuxStateMachine) -> None:
            pass

        async def async_cold_boot_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
            # We must use the stream. If it yields PanicDetected, we should raise.
            async for event in stream_mock.open_async():
                if isinstance(event, PanicDetected):
                    raise KernelPanicError(event.raw_output)

        async def async_resume_bootloader_to_os(self, fsm: EmbeddedLinuxStateMachine) -> None:
            pass
                    
    mock_cfg = MagicMock()
    mock_cfg.cold_boot_timeout_s = 60.0
    fsm = EmbeddedLinuxStateMachine(
        psu=MagicMock(),
        serial=serial_mock,
        ssh=MagicMock(),
        cfg=mock_cfg
    )
    fsm.event_stream = stream_mock
    fsm.boot_strategy = PanicBootStrategy()
    
    # Energize the board first
    fsm.energize()
    
    with pytest.raises(KernelPanicError, match="Device kernel panicked during OS boot sequence."):
        fsm.boot_to_os()

def test_fsm_timeout_during_wait_for_shell(monkeypatch):
    """Edge Case: The board hangs completely during boot, event stream times out."""
    monkeypatch.setattr('builtins.input', lambda _: None)
    from pytest_mes_core.transports import TransportTimeoutError
    
    serial_mock = MagicMock()
    stream_mock = MagicMock()
    
    # Mock open to yield nothing and just exit (simulating a timeout where the while loop ends)
    def mock_open(*args, **kwargs):
        return
        yield
        
    stream_mock.open.side_effect = mock_open
    
    mock_cfg = MagicMock()
    mock_cfg.cold_boot_timeout_s = 1.0
    
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
        fsm._event_wait_for_os_shell()

def test_fsm_hot_login_fallback_to_cold_boot(monkeypatch):
    """Edge Case: Hot login fails because the target doesn't show a shell prompt,
    so the FSM must fall back to a full cold boot power cycle."""
    monkeypatch.setattr('builtins.input', lambda _: None)
    from pytest_mes_core.transports import TransportTimeoutError
    
    serial_mock = MagicMock()
    stream_mock = MagicMock()
    
    # The first time we wait for shell (during hot login), it times out.
    # The second time (during cold boot), it succeeds.
    def mock_open_fail(*args, **kwargs):
        return
        yield
        
    def mock_open_success(*args, **kwargs):
        from pytest_mes_core.state_machine import PromptDetected
        import time
        yield PromptDetected(elapsed_s=time.time(), prompt_type="shell")
        
    stream_mock.open.side_effect = [mock_open_fail(), mock_open_success()]
    
    mock_cfg = MagicMock()
    mock_cfg.cold_boot_timeout_s = 1.0
    mock_cfg.os_user = "root"
    
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
    # Bypass the prompt that waits for input
    fsm._bypass_manual_prompts = True
    fsm.event_stream = stream_mock
    fsm.boot_strategy = MockBootStrategy()
    
    # Mock do_power_off and do_energize to not sleep for 3 seconds during tests
    fsm._do_power_off = MagicMock()
    fsm._do_energize = MagicMock()
    
    fsm.energize()
    
    # Act: call _hw_boot_to_os directly since it handles the fallback logic
    fsm._hw_boot_to_os(mock_event)
    
    # Assert that power cycle occurred because hot login failed
    fsm._do_power_off.assert_called()
