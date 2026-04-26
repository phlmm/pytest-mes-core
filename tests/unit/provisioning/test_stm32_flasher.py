import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# Mock pyocd before importing the provisioner
mock_pyocd = MagicMock()
sys.modules['pyocd'] = mock_pyocd
sys.modules['pyocd.flash'] = mock_pyocd.flash
sys.modules['pyocd.flash.file_programmer'] = mock_pyocd.flash.file_programmer

from pytest_mes_core.provisioning.stm32_flasher import Stm32Provisioner

def test_stm32_flash_firmware_file_not_found():
    swd = MagicMock()
    provisioner = Stm32Provisioner(swd)
    
    # Path doesn't exist
    assert not provisioner.flash_firmware(Path('/non/existent/fw.bin'))

@pytest.fixture(autouse=True)
def reset_mocks():
    mock_pyocd.reset_mock()

def test_stm32_flash_firmware_success():
    swd = MagicMock()
    swd.is_connected = True
    
    provisioner = Stm32Provisioner(swd)
    
    with patch('pathlib.Path.exists', return_value=True), patch('pathlib.Path.suffix', '.hex'):
        res = provisioner.flash_firmware(Path('/fake/fw.hex'))
    
    assert res is True
    swd.halt.assert_called_once()
    swd.reset.assert_called_once()
    swd.resume.assert_called_once()
    mock_pyocd.flash.file_programmer.FileProgrammer.return_value.program.assert_called_once_with('/fake/fw.hex')

@pytest.mark.anyio
async def test_async_stm32_flash_firmware():
    swd = MagicMock()
    swd.is_connected = False
    
    provisioner = Stm32Provisioner(swd)
    
    with patch('pathlib.Path.exists', return_value=True), patch('pathlib.Path.suffix', '.bin'):
        res = await provisioner.async_flash_firmware(Path('/fake/fw.bin'), base_address=0x08000000)
        
    assert res is True
    swd.connect.assert_called_once()
    swd.halt.assert_called_once()
    swd.reset.assert_called_once()
    mock_pyocd.flash.file_programmer.FileProgrammer.return_value.program.assert_called_once_with('/fake/fw.bin', base_address=0x08000000)
