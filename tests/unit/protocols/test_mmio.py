import pytest
from unittest.mock import MagicMock
from pytest_mes_core.protocols.mmio import MmioValidator
from pytest_mes_core.config import MmioConfig
from pytest_mes_core.transports import CommandResult, TransportTimeoutError, TransportConnectionError

def test_mmio_read_success_no_expected():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32, bit_mask_hex='0xFFFFFFFF')
    mock_dut.safe_run.return_value = CommandResult(command='devmem', ok=True, exited=0, stdout='0x12345678\n', stderr='', duration_s=0.05)
    res = MmioValidator.read_register(mock_dut, cfg)
    assert res.passed
    assert res.context['mmio_raw_val'] == '0X12345678'

def test_mmio_read_with_mask():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32, bit_mask_hex='0xFF000000')
    mock_dut.safe_run.return_value = CommandResult(command='devmem', ok=True, exited=0, stdout='0xABCDEF01\n', stderr='', duration_s=0.05)
    res = MmioValidator.read_register(mock_dut, cfg)
    assert res.passed
    assert res.context['mmio_masked_val'] == '0XAB000000'

def test_mmio_read_expected_value_match():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32, bit_mask_hex='0xFFFF0000', expected_value_hex='0xDEAD0000')
    mock_dut.safe_run.return_value = CommandResult(command='devmem', ok=True, exited=0, stdout='0xDEADBEEF\n', stderr='', duration_s=0.05)
    res = MmioValidator.read_register(mock_dut, cfg)
    assert res.passed

def test_mmio_read_expected_value_mismatch():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32, bit_mask_hex='0xFFFF0000', expected_value_hex='0xCAFE0000')
    mock_dut.safe_run.return_value = CommandResult(command='devmem', ok=True, exited=0, stdout='0xDEADBEEF\n', stderr='', duration_s=0.05)
    res = MmioValidator.read_register(mock_dut, cfg)
    assert not res.passed
    assert 'Mismatch' in res.error_msg

def test_mmio_read_strict_devmem_blocked():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32)
    mock_dut.safe_run.return_value = CommandResult(command='devmem', ok=False, exited=1, stdout='', stderr='Operation not permitted', duration_s=0.05)
    res = MmioValidator.read_register(mock_dut, cfg)
    assert not res.passed
    assert 'CONFIG_STRICT_DEVMEM' in res.error_msg

def test_mmio_read_bus_hang():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32)
    mock_dut.safe_run.side_effect = TransportTimeoutError('Bus hang')
    res = MmioValidator.read_register(mock_dut, cfg)
    assert not res.passed
    assert 'Bus Hang' in res.error_msg

def test_mmio_read_data_abort():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32)
    mock_dut.safe_run.side_effect = TransportConnectionError('Kernel panic')
    res = MmioValidator.read_register(mock_dut, cfg)
    assert not res.passed
    assert 'Data Abort' in res.error_msg

def test_mmio_write_success():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32)
    mock_dut.safe_run.return_value = CommandResult(command='devmem', ok=True, exited=0, stdout='', stderr='', duration_s=0.05)
    res = MmioValidator.write_register(mock_dut, cfg, '0xDEADBEEF')
    assert res.passed
    mock_dut.safe_run.assert_called_with('devmem 0x30390000 32 0xDEADBEEF', timeout_s=3.0)

def test_mmio_write_strict_devmem_blocked():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32)
    mock_dut.safe_run.return_value = CommandResult(command='devmem', ok=False, exited=1, stdout='', stderr='Operation not permitted', duration_s=0.05)
    res = MmioValidator.write_register(mock_dut, cfg, '0xDEADBEEF')
    assert not res.passed
    assert 'CONFIG_STRICT_DEVMEM' in res.error_msg

def test_mmio_write_bus_hang():
    mock_dut = MagicMock()
    cfg = MmioConfig(address_hex='0x30390000', data_width=32)
    mock_dut.safe_run.side_effect = TransportTimeoutError('Bus hang')
    res = MmioValidator.write_register(mock_dut, cfg, '0xDEADBEEF')
    assert not res.passed
    assert 'Bus Hang' in res.error_msg