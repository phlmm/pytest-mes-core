import pytest
from unittest.mock import MagicMock, patch
from pytest_mes_core.protocols.usb import UsbMassStorageValidator
from pytest_mes_core.config import UsbStorageConfig
from pytest_mes_core.transports import CommandResult, TransportConnectionError

@pytest.fixture
def usb_config():
    return UsbStorageConfig(vid_hex='0951', pid_hex='1666', test_size_mb=10, min_write_mbps=20.0)

def test_usb_validator_enumeration_failure(usb_config):
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='lsusb', ok=False, exited=1, stdout='', stderr='', duration_s=0.1)
    validator = UsbMassStorageValidator(mock_dut, usb_config)
    res = validator.verify_throughput_and_integrity()
    assert not res.passed
    assert 'not enumerated' in res.error_msg

def test_usb_validator_throughput_success(usb_config):
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('lsusb'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='Bus 001 Device 002: ID 0951:1666 Kingston Technology', stderr='', duration_s=0.1)
        elif cmd.startswith('ls -l /dev/disk/by-id'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='lrwxrwxrwx 1 root root 9 Jan 1 00:00 usb-Kingston_DataTraveler -> ../../sda1', stderr='', duration_s=0.1)
        elif cmd.startswith('dmesg -c'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        elif cmd.startswith('mkdir') or cmd.startswith('umount') or cmd.startswith('mount'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        elif cmd.startswith('dd if=/dev/urandom'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='10485760 bytes copied, 0.5 s, 50.0 MB/s', stderr='', duration_s=0.5)
        elif cmd.startswith('md5sum'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='hash  file', stderr='', duration_s=0.1)
        elif cmd.startswith('dmesg | grep'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        elif cmd.startswith('rm -f') or cmd.startswith('sync') or cmd.startswith('rm -rf'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    validator = UsbMassStorageValidator(mock_dut, usb_config)
    with patch('pytest_mes_core.protocols.usb.collect_soc_health', return_value={}):
        res = validator.verify_throughput_and_integrity()
    assert res.passed
    assert res.metrics['usb_write_mbps'] == 50.0
    mock_dut.safe_run.assert_any_call('dd if=/dev/urandom of=/tmp/usb_test_0951_1666/factory_test.bin bs=1M count=10 conv=fsync', timeout_s=45.0)

def test_usb_validator_degraded_speed(usb_config):
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('lsusb'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='Found', stderr='', duration_s=0.1)
        elif cmd.startswith('ls -l /dev/disk/by-id'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='-> ../../sda1', stderr='', duration_s=0.1)
        elif cmd.startswith('dd if=/dev/urandom'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='10485760 bytes copied, 2.0 s, 10.0 MB/s', stderr='', duration_s=2.0)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    validator = UsbMassStorageValidator(mock_dut, usb_config)
    with patch('pytest_mes_core.protocols.usb.collect_soc_health', return_value={}):
        res = validator.verify_throughput_and_integrity()
    assert not res.passed
    assert 'USB speed degraded' in res.error_msg
    assert res.metrics['usb_write_mbps'] == 10.0

def test_usb_validator_brownout_detected(usb_config):
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('lsusb'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='Found', stderr='', duration_s=0.1)
        elif cmd.startswith('ls -l /dev/disk/by-id'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='-> ../../sda1', stderr='', duration_s=0.1)
        elif cmd.startswith('dd if=/dev/urandom'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='10485760 bytes copied, 0.5 s, 50.0 MB/s', stderr='', duration_s=0.5)
        elif cmd.startswith('dmesg | grep'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='usb 1-1: USB disconnect', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    validator = UsbMassStorageValidator(mock_dut, usb_config)
    with patch('pytest_mes_core.protocols.usb.collect_soc_health', return_value={}):
        res = validator.verify_throughput_and_integrity()
    assert not res.passed
    assert 'brownout or EMI reset' in res.error_msg
    assert 'usb 1-1: USB disconnect' in res.context['brownout_trace']

def test_usb_validator_pmic_trip(usb_config):
    mock_dut = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('lsusb'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='Found', stderr='', duration_s=0.1)
        elif cmd.startswith('ls -l /dev/disk/by-id'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='-> ../../sda1', stderr='', duration_s=0.1)
        elif cmd.startswith('dd if=/dev/urandom'):
            raise TransportConnectionError('Socket shattered')
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    mock_dut.is_connected = True
    validator = UsbMassStorageValidator(mock_dut, usb_config)
    with patch('pytest_mes_core.protocols.usb.collect_soc_health', return_value={}):
        res = validator.verify_throughput_and_integrity()
    assert not res.passed
    assert 'Transport dropped' in res.error_msg