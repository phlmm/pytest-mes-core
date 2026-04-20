import pytest
from unittest.mock import MagicMock, call
from pytest_mes_core.protocols.uart_loopback import UartTopologyValidator
from pytest_mes_core.transports.base import CommandResult

@pytest.fixture
def mock_dut():
    return MagicMock()

@pytest.fixture
def host_adapters():
    return {"ttyUSB0": MagicMock()}

def test_configure_dut_interface_success(mock_dut):
    validator = UartTopologyValidator(dut=mock_dut)
    
    mock_dut.safe_run.return_value = CommandResult("", "", "", 0, True, 0.1)
    
    validator.configure_dut_interface("/dev/ttyS1", 115200)
    
    mock_dut.safe_run.assert_called_with("stty -F /dev/ttyS1 115200 raw -echo -echoe -echok -icrnl -onlcr clocal cread -crtscts")

def test_configure_dut_interface_fails(mock_dut):
    validator = UartTopologyValidator(dut=mock_dut)
    
    mock_dut.safe_run.return_value = CommandResult("", "", "stty: /dev/ttyS1: No such file or directory", 1, False, 0.1)
    
    with pytest.raises(RuntimeError, match="Failed to configure /dev/ttyS1"):
        validator.configure_dut_interface("/dev/ttyS1", 115200)

def test_fire_and_reap_dut_to_host(mock_dut, host_adapters):
    validator = UartTopologyValidator(dut=mock_dut, host_adapters=host_adapters)
    
    mock_dut.safe_run.return_value = CommandResult("", "", "", 0, True, 0.1)
    
    host_adapters["ttyUSB0"].expect.return_value = True
    
    res = validator._fire_and_reap("dut:/dev/ttyS1", ["host:ttyUSB0"], b"TEST", 1.0)
    
    assert res is True
    mock_dut.safe_run.assert_called_with("printf '\\x54\\x45\\x53\\x54' > /dev/ttyS1")
    host_adapters["ttyUSB0"].clear_rx_buffer.assert_called_once()
    host_adapters["ttyUSB0"].expect.assert_called_with(b"TEST", 1.0)

def test_fire_and_reap_host_to_dut_success(mock_dut, host_adapters):
    validator = UartTopologyValidator(dut=mock_dut, host_adapters=host_adapters)
    
    def safe_run_side_effect(cmd, **kwargs):
        if "hexdump" in cmd:
            return CommandResult(cmd, "54455354", "", 0, True, 0.1)
        return CommandResult(cmd, "", "", 0, True, 0.1)
        
    mock_dut.safe_run.side_effect = safe_run_side_effect
    
    res = validator._fire_and_reap("host:ttyUSB0", ["dut:/dev/ttyS2"], b"TEST", 1.0)
    
    assert res is True
    host_adapters["ttyUSB0"].send.assert_called_once_with(b"TEST")
    
    # Check daemon handling
    mock_dut.safe_run.assert_any_call("killall -9 cat")
    mock_dut.safe_run.assert_any_call("rm -f /tmp/uart_rx_dev_ttyS2.log")
    mock_dut.safe_run.assert_any_call("hexdump -v -e '/1 \"%02X\"' /tmp/uart_rx_dev_ttyS2.log")

def test_fire_and_reap_host_to_dut_fail(mock_dut, host_adapters):
    validator = UartTopologyValidator(dut=mock_dut, host_adapters=host_adapters)
    
    def safe_run_side_effect(cmd, **kwargs):
        if "hexdump" in cmd:
            return CommandResult(cmd, "0000", "", 0, True, 0.1)
        return CommandResult(cmd, "", "", 0, True, 0.1)
        
    mock_dut.safe_run.side_effect = safe_run_side_effect
    
    res = validator._fire_and_reap("host:ttyUSB0", ["dut:/dev/ttyS2"], b"TEST", 1.0)
    
    assert res is False

def test_validate_topology(mock_dut, host_adapters):
    validator = UartTopologyValidator(dut=mock_dut, host_adapters=host_adapters)
    
    # Mock fire_and_reap to return True
    validator._fire_and_reap = MagicMock(return_value=True)
    
    nodes = ["host:ttyUSB0", "dut:/dev/ttyS1"]
    
    res = validator.validate_topology(nodes, b"PING", 1.0)
    
    assert res is True
    assert validator._fire_and_reap.call_count == 2
