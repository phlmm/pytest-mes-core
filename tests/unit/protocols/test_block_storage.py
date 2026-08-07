import pytest
from unittest.mock import MagicMock, patch
from pytest_mes_core.protocols.block_storage import BlockDeviceValidator
from pytest_mes_core.transports import CommandResult, TransportTimeoutError

def test_measure_throughput_success():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('df -m'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='Filesystem 1M-blocks\n/dev/root 1024', stderr='', duration_s=0.1)
        elif cmd.startswith('dd if=/dev/urandom'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='104857600 bytes (105 MB, 100 MiB) copied, 1.5 s, 70.0 MB/s', stderr='', duration_s=1.5)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    with patch('pytest_mes_core.protocols.block_storage.collect_soc_health', return_value={}):
        res = BlockDeviceValidator.measure_throughput(mock_dut, '/mnt/sdcard', 100, 50.0)
    assert res.passed
    assert res.metrics['write_speed_mbps'] == 70.0

def test_measure_throughput_slow_degraded():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('df -m'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='Filesystem 1M-blocks\n/dev/root 1024', stderr='', duration_s=0.1)
        elif cmd.startswith('dd if=/dev/urandom'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='104857600 bytes (105 MB, 100 MiB) copied, 10.0 s, 10.5 MB/s', stderr='', duration_s=10.0)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    with patch('pytest_mes_core.protocols.block_storage.collect_soc_health', return_value={}):
        res = BlockDeviceValidator.measure_throughput(mock_dut, '/mnt/sdcard', 100, 50.0)
    assert not res.passed
    assert 'Degraded throughput' in res.error_msg
    assert res.metrics['write_speed_mbps'] == 10.5

def test_measure_throughput_disk_full():
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('df -m'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='Filesystem 1M-blocks\n/dev/root 1024', stderr='', duration_s=0.1)
        elif cmd.startswith('dd if=/dev/urandom'):
            return CommandResult(command=cmd, ok=False, exited=1, stdout="dd: error writing '/mnt/sdcard/.mes_eol_speed_test.bin': No space left on device", stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    with patch('pytest_mes_core.protocols.block_storage.collect_soc_health', return_value={}):
        res = BlockDeviceValidator.measure_throughput(mock_dut, '/mnt/sdcard', 100, 50.0)
    assert not res.passed
    assert 'False Negative: Disk is completely full' in res.error_msg

def test_verify_emmc_health_success():
    mock_dut = MagicMock()
    mock_stdout = '\nDevice life time estimation type A [SEC_COUNT: 0x01]\nDevice life time estimation type B [SEC_COUNT: 0x02]\nPre EOL information [PRE_EOL_INFO: 0x01]\n'

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('mmc extcsd read'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout=mock_stdout, stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = BlockDeviceValidator.verify_emmc_health(mock_dut, '/dev/mmcblk0')
    assert res.passed
    assert res.metrics['emmc_life_used_percent'] == 20.0

def test_verify_emmc_health_pre_eol_warning():
    mock_dut = MagicMock()
    mock_stdout = '\nDevice life time estimation type A [SEC_COUNT: 0x08]\nDevice life time estimation type B [SEC_COUNT: 0x08]\nPre EOL information [PRE_EOL_INFO: 0x03]\n'

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('mmc extcsd read'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout=mock_stdout, stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = BlockDeviceValidator.verify_emmc_health(mock_dut, '/dev/mmcblk0')
    assert not res.passed
    assert res.metrics['emmc_life_used_percent'] == 80.0
    assert 'Pre-EOL warning active' in res.error_msg