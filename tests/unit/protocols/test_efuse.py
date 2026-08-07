import pytest
from unittest.mock import MagicMock, patch
from pytest_mes_core.protocols.efuse import NvmemEfuseValidator
from pytest_mes_core.config import EfuseConfig
from pytest_mes_core.transports import CommandResult

@pytest.fixture
def efuse_cfg():
    return EfuseConfig(nvmem_path='/sys/bus/nvmem/devices/imx-ocotp0/nvmem', require_32bit_alignment=True)

def test_efuse_read_success(efuse_cfg):
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='hexdump', ok=True, exited=0, stdout='DEADBEEF', stderr='', duration_s=0.1)
    val = NvmemEfuseValidator.read_efuse(mock_dut, efuse_cfg, '0x10', 4)
    assert val == 'DEADBEEF'

def test_efuse_read_wrong_length(efuse_cfg):
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='hexdump', ok=True, exited=0, stdout='DEAD', stderr='', duration_s=0.1)
    with pytest.raises(IOError, match='expected 4'):
        NvmemEfuseValidator.read_efuse(mock_dut, efuse_cfg, '0x10', 4)

def test_efuse_read_failure(efuse_cfg):
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='hexdump', ok=False, exited=1, stdout='', stderr='Permission denied', duration_s=0.1)
    with pytest.raises(IOError, match='NVMEM read failed'):
        NvmemEfuseValidator.read_efuse(mock_dut, efuse_cfg, '0x10', 4)

@patch('pytest_mes_core.protocols.efuse.time.sleep')
def test_efuse_burn_idempotent_already_programmed(mock_sleep, efuse_cfg):
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='hexdump', ok=True, exited=0, stdout='DEADBEEF', stderr='', duration_s=0.1)
    res = NvmemEfuseValidator.burn_efuse(mock_dut, efuse_cfg, '0x10', 'DEADBEEF')
    assert res.passed
    assert res.context['status'] == 'already_burned'

@patch('pytest_mes_core.protocols.efuse.time.sleep')
def test_efuse_burn_dirty_region_rejected(mock_sleep, efuse_cfg):
    mock_dut = MagicMock()
    mock_dut.safe_run.return_value = CommandResult(command='hexdump', ok=True, exited=0, stdout='CAFEBABE', stderr='', duration_s=0.1)
    res = NvmemEfuseValidator.burn_efuse(mock_dut, efuse_cfg, '0x10', 'DEADBEEF')
    assert not res.passed
    assert 'dirty' in res.error_msg.lower()
    assert 'Cannot overwrite' in res.error_msg

@patch('pytest_mes_core.protocols.efuse.time.sleep')
def test_efuse_burn_success(mock_sleep, efuse_cfg):
    mock_dut = MagicMock()
    call_count = [0]

    def safe_run_side_effect(cmd, *args, **kwargs):
        call_count[0] += 1
        if 'hexdump' in cmd:
            if call_count[0] == 1:
                return CommandResult(command=cmd, ok=True, exited=0, stdout='00000000', stderr='', duration_s=0.1)
            else:
                return CommandResult(command=cmd, ok=True, exited=0, stdout='DEADBEEF', stderr='', duration_s=0.1)
        elif 'base64' in cmd or 'dd' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.2)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = NvmemEfuseValidator.burn_efuse(mock_dut, efuse_cfg, '0x10', 'DEADBEEF')
    assert res.passed
    assert 't_efuse_burn_s' in res.metrics

@patch('pytest_mes_core.protocols.efuse.time.sleep')
def test_efuse_burn_readback_mismatch(mock_sleep, efuse_cfg):
    mock_dut = MagicMock()
    call_count = [0]

    def safe_run_side_effect(cmd, *args, **kwargs):
        call_count[0] += 1
        if 'hexdump' in cmd:
            if call_count[0] == 1:
                return CommandResult(command=cmd, ok=True, exited=0, stdout='00000000', stderr='', duration_s=0.1)
            else:
                return CommandResult(command=cmd, ok=True, exited=0, stdout='DEAD0000', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_dut.safe_run.side_effect = safe_run_side_effect
    res = NvmemEfuseValidator.burn_efuse(mock_dut, efuse_cfg, '0x10', 'DEADBEEF')
    assert not res.passed
    assert 'verification failed' in res.error_msg.lower()