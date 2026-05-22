import queue
import pytest
from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine, KernelPanicError
from pytest_mes_core.config import StateMachineConfig
from pytest_mes_core.transports import EphemeralSerialClient, EphemeralSSHClient
from unittest.mock import MagicMock

@pytest.mark.anyio
async def test_fsm_panic_watchdog_catches_hab_events():
    """
    Simulates a Toradex/NXP i.MX BootROM throwing a Secure Boot violation.
    The FSM should catch 'HAB Events' and raise a KernelPanicError instantly
    instead of waiting for the full 60-second boot timeout.

    The UartEventStream now reads from a pub/sub queue (subscribe/unsubscribe)
    rather than raw_read_chunk, so we stub subscribe() to return a pre-filled
    queue.
    """
    mock_serial = MagicMock(spec=EphemeralSerialClient)
    mock_serial.is_connected = True

    from pytest_mes_core.utils.uart_parser import UartStreamParser
    mock_serial.parser = UartStreamParser()

    # Build a real queue with the simulated UART stream.
    rx_q = queue.Queue()
    rx_q.put(b"U-Boot 2022.04\r\nLoading Kernel...\r\nHAB Events: SEC_ERR Signature Verification Failed")
    # Sentinel: the queue.Empty exception from the next get() will naturally
    # end the iteration loop, but the PanicDetected will already have been yielded.

    mock_serial.subscribe.return_value = rx_q
    mock_serial.unsubscribe = MagicMock()

    mock_ssh = MagicMock(spec=EphemeralSSHClient)
    cfg = StateMachineConfig(enabled=True)

    fsm = EmbeddedLinuxStateMachine(
        psu=None,
        serial=mock_serial,
        ssh=mock_ssh,
        cfg=cfg
    )

    with pytest.raises(KernelPanicError, match="Device kernel panicked during OS boot sequence"):
        await fsm.event_wait_for_os_shell()

    # Verify the subscriber was properly cleaned up (zero-leakage)
    mock_serial.unsubscribe.assert_called_once_with(rx_q)
