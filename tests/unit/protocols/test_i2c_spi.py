import pytest
from unittest.mock import MagicMock, AsyncMock

from pytest_mes_core.transports.base import CommandResult
from pytest_mes_core.protocols.i2c_bus import I2cBus, _format_hex
from pytest_mes_core.protocols.spi_bus import SpiBus

def test_i2c_format_hex():
    assert _format_hex(0x42) == "0x42"
    assert _format_hex(66) == "0x42"
    assert _format_hex("42") == "0x42"
    assert _format_hex("0x42") == "0x42"

def test_i2c_detect_parsing():
    dut = MagicMock()
    i2c = I2cBus(dut)
    
    mock_stdout = """
WARNING! This program can confuse your I2C bus, cause data loss and worse!
I will probe file /dev/i2c-1.
I will probe address range 0x08-0x77.
Continue? [Y/n] 
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:                         -- -- -- -- -- -- -- -- 
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
40: -- -- 42 -- -- -- -- -- -- -- -- -- -- -- -- -- 
50: 50 -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
60: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- 
70: -- -- -- -- -- -- -- --                         
"""
    res = CommandResult(command="i2cdetect -y 1", stdout=mock_stdout, stderr="", exited=0, ok=True, duration_s=0.1)
    dut.safe_run.return_value = res
    
    devices = i2c.detect(1)
    assert devices == [0x42, 0x50]

@pytest.mark.anyio
async def test_i2c_async_get_byte():
    dut = MagicMock()
    dut.async_safe_run = AsyncMock(return_value=CommandResult(command="", stdout="0xAB\n", stderr="", exited=0, ok=True, duration_s=0.1))
    
    i2c = I2cBus(dut)
    val = await i2c.async_get_byte(1, 0x42, 0x05)
    
    assert val == 0xAB
    dut.async_safe_run.assert_awaited_once_with("i2cget -y 1 0x42 0x5", timeout_s=2.0, check_exit_code=True)

def test_spi_transfer_parsing():
    dut = MagicMock()
    spi = SpiBus(dut)
    
    # "od -An -v -t x1" prints space separated hex values
    mock_stdout = " 01  ff  a2\n"
    res = CommandResult(command="", stdout=mock_stdout, stderr="", exited=0, ok=True, duration_s=0.1)
    dut.safe_run.return_value = res
    
    rx_bytes = spi.transfer("/dev/spidev0.0", ["01", "FF", "A2"])
    assert rx_bytes == ["01", "FF", "A2"]

@pytest.mark.anyio
async def test_spi_async_config():
    dut = MagicMock()
    dut.async_safe_run = AsyncMock(return_value=CommandResult(command="", stdout="", stderr="", exited=0, ok=True, duration_s=0.1))
    
    spi = SpiBus(dut)
    await spi.async_config("/dev/spidev0.0", mode=3, speed_hz=500000)
    
    dut.async_safe_run.assert_awaited_once_with("spi-config -d /dev/spidev0.0 -m 3 -b 8 -s 500000", timeout_s=2.0, check_exit_code=True)
