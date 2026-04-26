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
    
    def mock_write(data, *args, **kwargs):
        resp = b""
        if b'\x03' in data:
            resp = b"\r\nroot@imx8:~# "
        elif b'printf' in data:
            resp = b"__MES_START_12345678__\nHello MES\n__MES_EXIT_12345678__:0\nroot@imx8:~# "
            
        if resp:
            def delayed_put():
                import time
                time.sleep(0.05)
                for q in getattr(client, '_subscribers', []):
                    q.put(resp)
            import threading
            threading.Thread(target=delayed_put).start()
        return len(data)

    mock_serial.write.side_effect = mock_write
    mock_serial.is_open = True
    
    client.ser = mock_serial
    client._is_connected = True
    
    # Mock the UUID generation so we get deterministic markers
    import uuid
    mock_uuid = MagicMock()
    mock_uuid.hex = "12345678"
    
    import pytest_mes_core.transports.serial_client
    original_uuid4 = pytest_mes_core.transports.serial_client.uuid.uuid4
    pytest_mes_core.transports.serial_client.uuid.uuid4 = lambda: mock_uuid
    
    try:
        result = client.safe_run("echo Hello MES", expected_prompt="root@")
            
        assert result.ok is True
        assert result.stdout.strip() == "Hello MES"
        
        # Verify \x03 was sent
        assert mock_serial.write.call_args_list[0] == call(b'\x03')
    finally:
        pytest_mes_core.transports.serial_client.uuid.uuid4 = original_uuid4

