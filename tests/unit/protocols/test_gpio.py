import pytest
from unittest.mock import MagicMock, patch
from pytest_mes_core.protocols.gpio import GpioEdgeValidator, GpioLedActuator, GpioLoopbackValidator
from pytest_mes_core.config import GpioEdgeConfig, GpioLedConfig, GpioLoopbackConfig
from pytest_mes_core.transports import CommandResult, TransportTimeoutError, TransportConnectionError

def test_edge_validator_button_press_success():
    mock_dut = MagicMock()
    cfg = GpioEdgeConfig(gpiochip=0, line=5, timeout_s=10, edge_type='rising-edge')
    mock_dut.safe_run.return_value = CommandResult(command='gpiomon', ok=True, exited=0, stdout='1 rising-edge', stderr='', duration_s=1.2)
    res = GpioEdgeValidator.verify_button_press(mock_dut, cfg)
    assert res.passed
    assert res.metrics['t_edge_response_s'] == 1.2
    mock_dut.safe_run.assert_called_once_with('gpiomon --num-events=1 --rising-edge gpiochip0 5', timeout_s=10)

def test_edge_validator_timeout_no_press():
    mock_dut = MagicMock()
    cfg = GpioEdgeConfig(gpiochip=0, line=5, timeout_s=5, edge_type='falling-edge')
    mock_dut.safe_run.side_effect = TransportTimeoutError('Timed out')
    res = GpioEdgeValidator.verify_button_press(mock_dut, cfg)
    assert not res.passed
    assert 'timeout' in res.error_msg.lower()

def test_edge_validator_irq_pulse():
    mock_dut = MagicMock()
    cfg = GpioEdgeConfig(gpiochip=1, line=12, timeout_s=3, edge_type='both-edges')
    mock_dut.safe_run.return_value = CommandResult(command='gpiomon', ok=True, exited=0, stdout='edge event', stderr='', duration_s=0.05)
    res = GpioEdgeValidator.await_interrupt_pulse(mock_dut, cfg)
    assert res.passed
    assert res.metrics['t_edge_response_s'] == 0.05

def test_edge_validator_invalid_edge_type():
    mock_dut = MagicMock()
    cfg = MagicMock()
    cfg.edge_type = 'invalid-edge'
    cfg.gpiochip = 0
    cfg.line = 1
    cfg.timeout_s = 5
    with pytest.raises(ValueError, match='Invalid edge_type'):
        GpioEdgeValidator.verify_button_press(mock_dut, cfg)

def test_edge_validator_transport_connection_error():
    mock_dut = MagicMock()
    cfg = GpioEdgeConfig(gpiochip=0, line=5, timeout_s=5, edge_type='rising-edge')
    mock_dut.safe_run.side_effect = TransportConnectionError('Socket died')
    res = GpioEdgeValidator.verify_button_press(mock_dut, cfg)
    assert not res.passed
    assert 'shattered' in res.error_msg.lower()

def test_led_actuator_set_high():
    mock_dut = MagicMock()
    cfg = GpioLedConfig(gpiochip=2, line=7)
    mock_dut.safe_run.return_value = CommandResult(command='gpioset', ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    GpioLedActuator.set_output(mock_dut, cfg, state=True)
    mock_dut.safe_run.assert_called_once_with('gpioset --mode=wait gpiochip2 7=1 >/dev/null 2>&1 &', timeout_s=2.0)

def test_led_actuator_set_low():
    mock_dut = MagicMock()
    cfg = GpioLedConfig(gpiochip=2, line=7)
    mock_dut.safe_run.return_value = CommandResult(command='gpioset', ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    GpioLedActuator.set_output(mock_dut, cfg, state=False)
    mock_dut.safe_run.assert_called_once_with('gpioset --mode=wait gpiochip2 7=0 >/dev/null 2>&1 &', timeout_s=2.0)

def test_led_teardown_zero_leakage():
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='killall', ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    GpioLedActuator.teardown_zero_leakage(mock_dut)
    mock_dut.safe_run.assert_called_once_with('killall -9 gpioset >/dev/null 2>&1 || true', timeout_s=3.0)

@patch('pytest_mes_core.protocols.gpio.time.sleep')
def test_loopback_success(mock_sleep):
    mock_dut = MagicMock()
    cfg = GpioLoopbackConfig(tx_gpiochip=0, tx_line=1, rx_gpiochip=0, rx_line=2, settling_time_s=0.05)

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'gpioget' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='1\n', stderr='', duration_s=0.05)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    res = GpioLoopbackValidator.verify_loopback(mock_dut, cfg, test_state=True)
    assert res.passed
    assert res.context['rx_val'] == 1

@patch('pytest_mes_core.protocols.gpio.time.sleep')
def test_loopback_mismatch_broken_trace(mock_sleep):
    mock_dut = MagicMock()
    cfg = GpioLoopbackConfig(tx_gpiochip=0, tx_line=1, rx_gpiochip=0, rx_line=2, settling_time_s=0.05)

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'gpioget' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='0\n', stderr='', duration_s=0.05)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    res = GpioLoopbackValidator.verify_loopback(mock_dut, cfg, test_state=True)
    assert not res.passed
    assert 'mismatch' in res.error_msg.lower()
    assert res.context['rx_val'] == 0