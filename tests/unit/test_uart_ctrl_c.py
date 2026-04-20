import pytest
from unittest.mock import MagicMock, call
from pytest_mes_core.transports.serial_client import EphemeralSerialClient
from pytest_mes_core.config import HostSerialConfig

def test_serial_safe_run_handles_ctrl_c_stale_buffers():
    """
    Edge Case: Unflushed buffers from too many Ctrl+C interrupts.
    """
    cfg = HostSerialConfig(port="/dev/ttyUSB0", baudrate=115200)
    client = EphemeralSerialClient(cfg)
    
    mock_serial = MagicMock()
    
    # We will simulate the `expect` calls dynamically.
    # When `\x03` is written, expect will wait for `root@`.
    # When the command is written, expect will wait for `root@` again.
    
    read_sequence = [
        b"\r\nroot@imx8:~# ",   # Response to the \x03 (Ctrl+C)
        b"__MES_START_12345678__\nHello MES\n__MES_EXIT_12345678__:0\nroot@imx8:~# " # The actual command output
    ]
    
    def mock_in_waiting_prop():
        return len(read_sequence[0]) if read_sequence else 0
        
    def mock_read(size=1, *args, **kwargs):
        if read_sequence:
            return read_sequence.pop(0)
        return b""
        
    mock_serial.read.side_effect = mock_read
    mock_serial.is_open = True
    type(mock_serial).in_waiting = property(lambda self: mock_in_waiting_prop())
    
    client.ser = mock_serial
    
    # Mock the UUID generation so we get deterministic markers
    import uuid
    mock_uuid = MagicMock()
    mock_uuid.hex = "12345678"
    
    import pytest_mes_core.transports.serial_client
    original_uuid4 = pytest_mes_core.transports.serial_client.uuid.uuid4
    pytest_mes_core.transports.serial_client.uuid.uuid4 = lambda: mock_uuid
    
    try:
        with client.execution_lock():
            result = client.safe_run("echo Hello MES", expected_prompt="root@")
            
        assert result.ok is True
        assert result.stdout.strip() == "Hello MES"
        
        # Verify \x03 was sent
        assert mock_serial.write.call_args_list[0] == call(b'\x03')
        assert mock_serial.reset_input_buffer.call_count >= 2
    finally:
        pytest_mes_core.transports.serial_client.uuid.uuid4 = original_uuid4
