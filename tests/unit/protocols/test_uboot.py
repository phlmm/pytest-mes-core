import pytest
from unittest.mock import MagicMock, AsyncMock
from pytest_mes_core.transports.base import CommandResult
from pytest_mes_core.protocols.boot_env import UBootShell

def test_uboot_get_var():
    dut = MagicMock()
    mock_stdout = "bootcmd=run distro_bootcmd\n"
    dut.safe_run.return_value = CommandResult(command="", stdout=mock_stdout, stderr="", exited=0, ok=True, duration_s=0.1)
    
    shell = UBootShell(dut)
    val = shell.get_var("bootcmd")
    
    assert val == "run distro_bootcmd"
    dut.safe_run.assert_called_once_with("printenv bootcmd", timeout_s=2.0, expected_prompt="=>")

def test_uboot_get_var_not_found():
    dut = MagicMock()
    mock_stdout = "## Error: \"nonexistent\" not defined\n"
    dut.safe_run.return_value = CommandResult(command="", stdout=mock_stdout, stderr="", exited=0, ok=True, duration_s=0.1)
    
    shell = UBootShell(dut)
    val = shell.get_var("nonexistent")
    
    assert val is None

@pytest.mark.anyio
async def test_uboot_async_tftp_boot():
    dut = MagicMock()
    mock_stdout = "Using FEC device\nTFTP from server 192.168.1.1; our IP address is 192.168.1.10\nFilename 'Image'.\nLoad address: 0x40480000\nLoading: #####################\nBytes transferred = 31313408 (1ddd600 hex)\n"
    dut.async_safe_run = AsyncMock(return_value=CommandResult(command="", stdout=mock_stdout, stderr="", exited=0, ok=True, duration_s=0.1))
    
    shell = UBootShell(dut)
    success = await shell.async_tftp_boot("Image", "0x40480000")
    
    assert success is True
    dut.async_safe_run.assert_awaited_once_with("tftpboot 0x40480000 Image", timeout_s=60.0, expected_prompt="=>")

@pytest.mark.anyio
async def test_uboot_async_nfs_mount():
    dut = MagicMock()
    # Mock sequence: set_var, get_var, set_var
    # We will mock the `async_get_var` and `async_set_var` to isolate `async_nfs_mount`
    shell = UBootShell(dut)
    shell.async_get_var = AsyncMock(return_value="console=ttyS0,115200")
    shell.async_set_var = AsyncMock()
    
    await shell.async_nfs_mount("10.0.0.5", "/srv/nfs/rootfs")
    
    # 1. sets rootpath
    shell.async_set_var.assert_any_call("rootpath", "10.0.0.5:/srv/nfs/rootfs")
    # 2. fetches bootargs
    shell.async_get_var.assert_awaited_once_with("bootargs")
    # 3. updates bootargs
    shell.async_set_var.assert_any_call("bootargs", "console=ttyS0,115200 root=/dev/nfs nfsroot=${serverip}:${rootpath},v3,tcp rw ip=dhcp")
