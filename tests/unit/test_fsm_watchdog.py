import pytest
from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine
from pytest_mes_core.config import StateMachineConfig
from pytest_mes_core.transports import EphemeralSerialClient, EphemeralSSHClient
from tests.mocks.virtual_transport import MockTransport
from unittest.mock import MagicMock

def test_fsm_panic_watchdog_catches_hab_events():
    """
    Simulates a Toradex/NXP i.MX BootROM throwing a Secure Boot violation.
    The FSM should catch 'HAB Events' and raise a RuntimeError instantly
    instead of waiting for the full 60-second boot timeout.
    """
    mock_serial = MagicMock(spec=EphemeralSerialClient)
    mock_serial.is_connected = True
    
    # We need to mock the underlying 'ser' and 'parser' objects
    mock_serial.ser = MagicMock()
    mock_serial.parser = MagicMock()
    mock_serial.parser.extract_lines.return_value = []
    
    # Simulate a stream that prints normal boot text, then a HAB exception
    mock_serial.raw_read_chunk.side_effect = [
        b"U-Boot 2022.04\r\nLoading Kernel...\r\nHAB Events: SEC_ERR Signature Verification Failed",
        b"", b"", b"", b""
    ]
    
    mock_ssh = MagicMock(spec=EphemeralSSHClient)
    cfg = StateMachineConfig(enabled=True)
    
    fsm = EmbeddedLinuxStateMachine(
        psu=None,
        serial=mock_serial,
        ssh=mock_ssh,
        cfg=cfg
    )
    
    with pytest.raises(RuntimeError, match="Device kernel panicked during OS boot sequence"):
        # We manually invoke the boot sequence reader
        fsm._do_wait_for_os()
