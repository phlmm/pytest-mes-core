import pytest
from unittest.mock import MagicMock
from pytest_mes_core.protocols.sysfs_poller import BackgroundSysfsPoller
from pytest_mes_core.config.protocols import SysfsPollerConfig, SysfsTargetConfig
from pytest_mes_core.transports import CommandResult

@pytest.fixture
def sysfs_cfg():
    return SysfsPollerConfig(polling_interval_s=1.0, targets={'cpu_temp': SysfsTargetConfig(path='/sys/class/thermal/thermal_zone0/temp', scale=0.001, unit='C'), 'cpu_freq': SysfsTargetConfig(path='/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', scale=0.001, unit='MHz')})

def test_parse_data_numeric_aggregation(sysfs_cfg):
    mock_dut = MagicMock()
    poller = BackgroundSysfsPoller(mock_dut, sysfs_cfg)
    raw_lines = ['45000 || 1200000', '46000 || 1400000', '44000 || 1000000']
    metrics = poller._parse_data(raw_lines)
    assert metrics['cpu_temp_min_C'] == 44.0
    assert metrics['cpu_temp_max_C'] == 46.0
    assert metrics['cpu_temp_avg_C'] == 45.0
    assert metrics['cpu_freq_min_MHz'] == 1000.0
    assert metrics['cpu_freq_max_MHz'] == 1400.0
    assert metrics['cpu_freq_avg_MHz'] == 1200.0

def test_parse_data_empty(sysfs_cfg):
    mock_dut = MagicMock()
    poller = BackgroundSysfsPoller(mock_dut, sysfs_cfg)
    metrics = poller._parse_data([])
    assert metrics == {}
    metrics = poller._parse_data([''])
    assert metrics == {}

def test_parse_data_malformed_lines_skipped(sysfs_cfg):
    mock_dut = MagicMock()
    poller = BackgroundSysfsPoller(mock_dut, sysfs_cfg)
    raw_lines = ['45000 || 1200000', 'garbage line without separator', '46000 || 1400000']
    metrics = poller._parse_data(raw_lines)
    assert metrics['cpu_temp_min_C'] == 45.0
    assert metrics['cpu_temp_max_C'] == 46.0

def test_parse_data_string_fallback(sysfs_cfg):
    """When values can't be parsed as floats, fall back to string state."""
    mock_dut = MagicMock()
    poller = BackgroundSysfsPoller(mock_dut, sysfs_cfg)
    raw_lines = ['suspended || 1200000']
    metrics = poller._parse_data(raw_lines)
    assert metrics['cpu_temp_final_state'] == 'suspended'
    assert metrics['cpu_freq_avg_MHz'] == 1200.0

def test_context_manager_enter_serial_fallback(sysfs_cfg):
    """On a non-SSH transport, it should NOT create a HostSideBuffer."""
    mock_dut = MagicMock()
    mock_dut.__class__.__name__ = 'EphemeralSerialClient'
    mock_dut.safe_run.return_value = CommandResult(command='nohup', ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    poller = BackgroundSysfsPoller(mock_dut, sysfs_cfg)
    result = poller.__enter__()
    assert result is poller
    assert poller._buffer is None

def test_context_manager_exit_serial_fetch(sysfs_cfg):
    mock_dut = MagicMock()
    mock_dut.__class__.__name__ = 'EphemeralSerialClient'
    poller = BackgroundSysfsPoller(mock_dut, sysfs_cfg)
    poller._buffer = None

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'cat' in cmd and 'sysfs' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='45000 || 1200000\n46000 || 1400000\n', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    poller.__exit__(None, None, None)
    assert isinstance(poller.metrics, dict)