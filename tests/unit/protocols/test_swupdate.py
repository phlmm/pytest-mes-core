import pytest
from unittest.mock import MagicMock
from pytest_mes_core.protocols.swupdate import SWUpdateValidator
from pytest_mes_core.transports import CommandResult

def test_swupdate_pre_flight_checks():
    mock_dut = MagicMock()
    mock_fsm = MagicMock()
    mock_fsm.context.active_rootfs = '/dev/mmcblk0p2'
    mock_dut.safe_run.return_value = CommandResult(command='which', ok=True, exited=0, stdout='/usr/bin/swupdate\n', stderr='', duration_s=0.1)
    validator = SWUpdateValidator(mock_dut, mock_fsm)
    rootfs = validator.pre_flight_checks()
    assert rootfs == '/dev/mmcblk0p2'
    mock_dut.safe_run.assert_called_with('which swupdate', timeout_s=2.0, check_exit_code=True)

def test_swupdate_install_from_url():
    mock_dut = MagicMock()
    mock_fsm = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='swupdate', ok=True, exited=0, stdout='Installation successful', stderr='', duration_s=5.0)
    validator = SWUpdateValidator(mock_dut, mock_fsm)
    validator.install_from_url('http://example.com/update.swu', 60.0)
    mock_dut.safe_run.assert_called_with("swupdate -v -d '-u http://example.com/update.swu'", timeout_s=60.0, check_exit_code=True)

def test_swupdate_install_from_local_media():
    mock_dut = MagicMock()
    mock_fsm = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if cmd.startswith('ls'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        elif cmd.startswith('swupdate -i'):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='Installation successful', stderr='', duration_s=5.0)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    validator = SWUpdateValidator(mock_dut, mock_fsm)
    validator.install_from_local_media('/run/media/sda1/update.swu', 60.0)
    mock_dut.safe_run.assert_any_call('ls /run/media/sda1/update.swu', timeout_s=2.0, check_exit_code=True)
    mock_dut.safe_run.assert_any_call('swupdate -i /run/media/sda1/update.swu -v', timeout_s=60.0, check_exit_code=True)

def test_swupdate_verify_partition_flip_success():
    mock_dut = MagicMock()
    mock_fsm = MagicMock()
    original_rootfs = '/dev/mmcblk0p2'

    def boot_to_os_side_effect():
        mock_fsm.context.active_rootfs = '/dev/mmcblk0p3'
    mock_fsm.machine.boot_to_os.side_effect = boot_to_os_side_effect
    mock_dut.safe_run.return_value = CommandResult(command='swupdate', ok=True, exited=0, stdout='Testing', stderr='', duration_s=0.1)
    validator = SWUpdateValidator(mock_dut, mock_fsm)
    validator.verify_partition_flip(original_rootfs)
    mock_fsm.machine.mark_dirty.assert_called_once()
    mock_fsm.machine.boot_to_os.assert_called_once()
    mock_dut.safe_run.assert_any_call('fw_setenv upgrade_available 0', timeout_s=3.0, check_exit_code=False)
    mock_dut.safe_run.assert_any_call('fw_setenv bootcount 0', timeout_s=3.0, check_exit_code=False)

def test_swupdate_verify_partition_flip_revert():
    mock_dut = MagicMock()
    mock_fsm = MagicMock()
    original_rootfs = '/dev/mmcblk0p2'

    def boot_to_os_side_effect():
        mock_fsm.context.active_rootfs = '/dev/mmcblk0p2'
    mock_fsm.machine.boot_to_os.side_effect = boot_to_os_side_effect
    validator = SWUpdateValidator(mock_dut, mock_fsm)
    with pytest.raises(RuntimeError, match='The update was rejected by U-Boot'):
        validator.verify_partition_flip(original_rootfs)

def test_swupdate_execute_full_ota():
    mock_dut = MagicMock()
    mock_fsm = MagicMock()
    validator = SWUpdateValidator(mock_dut, mock_fsm)
    validator.pre_flight_checks = MagicMock(return_value='/dev/mmcblk0p2')
    validator.install_from_url = MagicMock()
    validator.verify_partition_flip = MagicMock()
    validator.execute_full_ota('http://test.url', 100.0)
    validator.pre_flight_checks.assert_called_once()
    validator.install_from_url.assert_called_with('http://test.url', 100.0)
    validator.verify_partition_flip.assert_called_with('/dev/mmcblk0p2')