import pytest
from unittest.mock import MagicMock, patch
from pytest_mes_core.protocols.executable import CustomPayloadValidator, FATAL_SIGNALS
from pytest_mes_core.config import ExecutableConfig
from pytest_mes_core.transports import CommandResult, TransportTimeoutError, TransportConnectionError

def test_run_binary_success():
    mock_dut = MagicMock()
    cfg = ExecutableConfig(binary_path='/usr/bin/hw_selftest', arguments='--mode=quick', timeout_s=30.0)

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'test -x' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        elif '/usr/bin/hw_selftest' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='ALL TESTS PASSED', stderr='', duration_s=2.5)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    res = CustomPayloadValidator.run_binary(mock_dut, cfg)
    assert res.passed
    assert res.metrics['t_hw_selftest_exec_s'] == 2.5

def test_run_binary_not_found():
    mock_dut = MagicMock()
    cfg = ExecutableConfig(binary_path='/usr/bin/missing', timeout_s=5.0)
    mock_dut.safe_run.return_value = CommandResult(command='test', ok=False, exited=1, stdout='', stderr='', duration_s=0.1)
    mock_dut.is_connected = True
    res = CustomPayloadValidator.run_binary(mock_dut, cfg)
    assert not res.passed
    assert 'not found' in res.error_msg.lower()

def test_run_binary_wrong_exit_code():
    mock_dut = MagicMock()
    cfg = ExecutableConfig(binary_path='/usr/bin/hw_selftest', expected_exit_code=0, timeout_s=30.0)

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'test -x' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        elif '/usr/bin/hw_selftest' in cmd:
            return CommandResult(command=cmd, ok=False, exited=42, stdout='Test 3 failed', stderr='', duration_s=1.0)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    res = CustomPayloadValidator.run_binary(mock_dut, cfg)
    assert not res.passed
    assert '42' in res.error_msg

def test_run_binary_segfault():
    mock_dut = MagicMock()
    cfg = ExecutableConfig(binary_path='/usr/bin/hw_selftest', timeout_s=30.0)

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'test -x' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        elif '/usr/bin/hw_selftest' in cmd:
            return CommandResult(command=cmd, ok=False, exited=139, stdout='', stderr='', duration_s=0.1)
        elif 'dmesg' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='traps: hw_selftest[1234] segfault at 0x0', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    res = CustomPayloadValidator.run_binary(mock_dut, cfg)
    assert not res.passed
    assert 'SIGSEGV' in res.error_msg
    assert 'kernel_trap_trace' in res.context

def test_run_binary_oom_killed():
    mock_dut = MagicMock()
    cfg = ExecutableConfig(binary_path='/usr/bin/hw_selftest', timeout_s=30.0)

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'test -x' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        elif '/usr/bin/hw_selftest' in cmd:
            return CommandResult(command=cmd, ok=False, exited=137, stdout='', stderr='', duration_s=0.1)
        elif 'dmesg' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='Out of memory: Killed process 1234', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    res = CustomPayloadValidator.run_binary(mock_dut, cfg)
    assert not res.passed
    assert 'SIGKILL' in res.error_msg

def test_run_binary_timeout_lockup():
    mock_dut = MagicMock()
    cfg = ExecutableConfig(binary_path='/usr/bin/hw_selftest', timeout_s=10.0)

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'test -x' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        elif '/usr/bin/hw_selftest' in cmd:
            raise TransportTimeoutError('Timed out')
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    res = CustomPayloadValidator.run_binary(mock_dut, cfg)
    assert not res.passed
    assert 'locked up' in res.error_msg.lower()

def test_fatal_signal_map_completeness():
    """Verify the POSIX signal map covers the critical crash signals."""
    assert 132 in FATAL_SIGNALS
    assert 134 in FATAL_SIGNALS
    assert 135 in FATAL_SIGNALS
    assert 136 in FATAL_SIGNALS
    assert 137 in FATAL_SIGNALS
    assert 139 in FATAL_SIGNALS