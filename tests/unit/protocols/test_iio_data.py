import pytest
from unittest.mock import MagicMock
from pytest_mes_core.protocols.iio_data import IioAdcValidator, IioDacActuator
from pytest_mes_core.transports import CommandResult, TransportTimeoutError

def test_adc_measure_voltage_success():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'grep -l' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='/sys/bus/iio/devices/iio:device0/name\n', stderr='', duration_s=0.1)
        elif 'in_voltage0_scale' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='0.805664\n', stderr='', duration_s=0.1)
        elif 'for i in' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='2048\n' * 10, stderr='', duration_s=0.2)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = IioAdcValidator.measure_voltage(mock_dut, 'ads1015', 0, samples=10, min_v=1.0, max_v=2.0)
    assert res.passed
    assert abs(res.metrics['voltage_v'] - 1.65) < 0.01

def test_adc_sensor_not_found():
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='grep', ok=False, exited=1, stdout='', stderr='', duration_s=0.1)
    res = IioAdcValidator.measure_voltage(mock_dut, 'missing_sensor', 0)
    assert not res.passed
    assert 'not found in sysfs' in res.error_msg

def test_adc_voltage_out_of_bounds():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'grep -l' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='/sys/bus/iio/devices/iio:device0/name\n', stderr='', duration_s=0.1)
        elif 'in_voltage0_scale' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='0.805664\n', stderr='', duration_s=0.1)
        elif 'for i in' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='4095\n' * 10, stderr='', duration_s=0.2)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = IioAdcValidator.measure_voltage(mock_dut, 'ads1015', 0, samples=10, min_v=1.0, max_v=2.0)
    assert not res.passed
    assert 'out of bounds' in res.error_msg.lower()

def test_adc_bus_lockup():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'grep -l' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='/sys/bus/iio/devices/iio:device0/name\n', stderr='', duration_s=0.1)
        elif 'in_voltage0_scale' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='0.805664\n', stderr='', duration_s=0.1)
        elif 'for i in' in cmd:
            raise TransportTimeoutError('I2C bus locked')
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = IioAdcValidator.measure_voltage(mock_dut, 'ads1015', 0, samples=10)
    assert not res.passed
    assert 'hung during ADC' in res.error_msg

def test_adc_sample_count_mismatch():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'grep -l' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='/sys/bus/iio/devices/iio:device0/name\n', stderr='', duration_s=0.1)
        elif 'in_voltage0_scale' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='0.805664\n', stderr='', duration_s=0.1)
        elif 'for i in' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='2048\n' * 5, stderr='', duration_s=0.2)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = IioAdcValidator.measure_voltage(mock_dut, 'ads1015', 0, samples=10)
    assert not res.passed
    assert 'Burst mismatch' in res.error_msg

def test_dac_set_voltage_success():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'grep -l' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='/sys/bus/iio/devices/iio:device1/name\n', stderr='', duration_s=0.1)
        elif 'out_voltage0_scale' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='0.805664\n', stderr='', duration_s=0.1)
        elif 'echo' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.05)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = IioDacActuator.set_voltage(mock_dut, 'dac7571', 0, target_v=1.65)
    assert res.passed
    assert 't_dac_set_s' in res.metrics

def test_dac_sensor_not_found():
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='grep', ok=False, exited=1, stdout='', stderr='', duration_s=0.1)
    res = IioDacActuator.set_voltage(mock_dut, 'missing_dac', 0, target_v=1.0)
    assert not res.passed
    assert 'not found in sysfs' in res.error_msg

def test_dac_write_rejected():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'grep -l' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='/sys/bus/iio/devices/iio:device1/name\n', stderr='', duration_s=0.1)
        elif 'out_voltage0_scale' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='0.805664\n', stderr='', duration_s=0.1)
        elif 'echo' in cmd:
            return CommandResult(command=cmd, ok=False, exited=1, stdout='', stderr='Invalid argument', duration_s=0.05)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = IioDacActuator.set_voltage(mock_dut, 'dac7571', 0, target_v=1.65)
    assert not res.passed
    assert 'Failed to write DAC' in res.error_msg