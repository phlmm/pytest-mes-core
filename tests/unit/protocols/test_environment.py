import pytest
from unittest.mock import MagicMock
from pytest_mes_core.protocols.environment import EnvironmentValidator
from pytest_mes_core.transports import CommandResult, TransportTimeoutError

def test_verify_dependencies_all_present():
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='for', ok=True, exited=0, stdout='', stderr='', duration_s=0.5)
    res = EnvironmentValidator.verify_target_dependencies(mock_dut)
    assert res.passed

def test_verify_dependencies_missing_binaries():
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='for', ok=True, exited=0, stdout='iperf3\ncansend\n', stderr='', duration_s=0.5)
    res = EnvironmentValidator.verify_target_dependencies(mock_dut)
    assert not res.passed
    assert 'iperf3' in res.error_msg
    assert 'cansend' in res.error_msg
    assert res.context['missing_binaries'] == ['iperf3', 'cansend']

def test_verify_dependencies_with_extras():
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='for', ok=True, exited=0, stdout='', stderr='', duration_s=0.5)
    res = EnvironmentValidator.verify_target_dependencies(mock_dut, extra_binaries=['custom_tool'])
    assert res.passed
    call_args = mock_dut.safe_run.call_args[0][0]
    assert 'custom_tool' in call_args

def test_verify_dependencies_dut_hung():
    mock_dut = MagicMock()
    mock_dut.safe_run.side_effect = TransportTimeoutError('Timed out')
    res = EnvironmentValidator.verify_target_dependencies(mock_dut)
    assert not res.passed
    assert 'hung completely' in res.error_msg.lower()

def test_system_health_clean_kernel():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'dmesg' in cmd:
            return CommandResult(command=cmd, ok=True, exited=1, stdout='', stderr='', duration_s=0.1)
        elif 'date +%Y' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='2026\n', stderr='', duration_s=0.1)
        elif 'nproc' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='4\n', stderr='', duration_s=0.1)
        elif '/proc/loadavg' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='0.15 0.03 0.01 1/141 1234\n', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = EnvironmentValidator.verify_system_health(mock_dut)
    assert res.passed
    assert res.metrics['baseline_cpu_load'] == 0.15
    assert res.context['cpu_core_count'] == 4

def test_system_health_kernel_panic_detected():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'dmesg' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='[  1.234] kernel BUG at mm/slab.c:1234!\n', stderr='', duration_s=0.1)
        elif 'date +%Y' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='2026\n', stderr='', duration_s=0.1)
        elif 'nproc' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='4\n', stderr='', duration_s=0.1)
        elif '/proc/loadavg' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='0.15 0.03 0.01 1/141 1234\n', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = EnvironmentValidator.verify_system_health(mock_dut)
    assert not res.passed
    assert 'panics detected' in res.error_msg.lower()
    assert 'kernel BUG' in res.context['early_boot_panics']

def test_system_health_high_cpu_load():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'dmesg' in cmd:
            return CommandResult(command=cmd, ok=True, exited=1, stdout='', stderr='', duration_s=0.1)
        elif 'date +%Y' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='2026\n', stderr='', duration_s=0.1)
        elif 'nproc' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='2\n', stderr='', duration_s=0.1)
        elif '/proc/loadavg' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='8.5 5.0 3.0 1/141 1234\n', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = EnvironmentValidator.verify_system_health(mock_dut)
    assert res.passed
    assert res.context.get('cpu_thrashing_detected') is True