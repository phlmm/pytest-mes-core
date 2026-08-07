import pytest
from unittest.mock import MagicMock, call
from pytest_mes_core.protocols.can_bus import CanTopologyValidator
from pytest_mes_core.transports.base import CommandResult

@pytest.fixture
def mock_dut():
    return MagicMock()

@pytest.fixture
def host_adapters():
    return {'pcan0': MagicMock()}

def test_configure_dut_interface_success(mock_dut):
    validator = CanTopologyValidator(dut=mock_dut)

    def safe_run_side_effect(cmd, **kwargs):
        if 'ip link show' in cmd:
            return CommandResult(cmd, 'can0: <NOARP,UP,LOWER_UP> mtu 16', '', 0, True, 0.1)
        return CommandResult(cmd, '', '', 0, True, 0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    validator.configure_dut_interface('can0', 500000)
    calls = [call('ip link set can0 down'), call('ip link set can0 type can bitrate 500000'), call('ip link set can0 up'), call('ip link show can0')]
    mock_dut.safe_run.assert_has_calls(calls, any_order=False)

def test_configure_dut_interface_fails_bitrate(mock_dut):
    validator = CanTopologyValidator(dut=mock_dut)

    def safe_run_side_effect(cmd, **kwargs):
        if 'bitrate' in cmd:
            return CommandResult(cmd, '', 'Invalid bitrate', 1, False, 0.1)
        return CommandResult(cmd, '', '', 0, True, 0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    with pytest.raises(RuntimeError, match='Failed to set bitrate'):
        validator.configure_dut_interface('can0', 500000)

def test_configure_dut_interface_fails_up(mock_dut):
    validator = CanTopologyValidator(dut=mock_dut)

    def safe_run_side_effect(cmd, **kwargs):
        if 'show' in cmd:
            return CommandResult(cmd, 'can0: <NOARP,DOWN> mtu 16', '', 0, True, 0.1)
        return CommandResult(cmd, '', '', 0, True, 0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    with pytest.raises(RuntimeError, match='refused to transition to UP state'):
        validator.configure_dut_interface('can0', 500000)

def test_teardown_dut_interface(mock_dut):
    validator = CanTopologyValidator(dut=mock_dut)
    validator.teardown_dut_interface('can1')
    mock_dut.safe_run.assert_called_with('ip link set can1 down')

def test_fire_and_reap_dut_to_host(mock_dut, host_adapters):
    validator = CanTopologyValidator(dut=mock_dut, host_adapters=host_adapters)
    mock_dut.safe_run.return_value = CommandResult('', '', '', 0, True, 0.1)
    host_adapters['pcan0'].expect.return_value = True
    res = validator._fire_and_reap('dut:can0', ['host:pcan0'], 123, b'\x01\x02\x03\x04', 1.0)
    assert res is True
    mock_dut.safe_run.assert_called_with('cansend can0 07B#01020304')
    host_adapters['pcan0'].clear_rx_buffer.assert_called_once()
    host_adapters['pcan0'].expect.assert_called_with(123, b'\x01\x02\x03\x04', 1.0)

def test_fire_and_reap_host_to_dut(mock_dut, host_adapters):
    validator = CanTopologyValidator(dut=mock_dut, host_adapters=host_adapters)

    def safe_run_side_effect(cmd, **kwargs):
        if 'cat /tmp/can_rx_can1.log' in cmd:
            return CommandResult(cmd, '  can1  07B   [4]  01 02 03 04', '', 0, True, 0.1)
        return CommandResult(cmd, '', '', 0, True, 0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = validator._fire_and_reap('host:pcan0', ['dut:can1'], 123, b'\x01\x02\x03\x04', 1.0)
    assert res is True
    host_adapters['pcan0'].send.assert_called_once_with(123, b'\x01\x02\x03\x04')
    mock_dut.safe_run.assert_any_call('killall -9 candump')
    mock_dut.safe_run.assert_any_call('rm -f /tmp/can_rx_can1.log')

def test_validate_topology(mock_dut, host_adapters):
    validator = CanTopologyValidator(dut=mock_dut, host_adapters=host_adapters)
    validator._fire_and_reap = MagicMock(return_value=True)
    nodes = ['host:pcan0', 'dut:can0']
    validator.validate_topology(nodes, 123, b'\x01', 1.0)
    assert validator._fire_and_reap.call_count == 2