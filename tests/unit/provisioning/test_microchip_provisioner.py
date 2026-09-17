import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pytest_mes_core.host_adapters.microchip import HostPickitAdapter
from pytest_mes_core.provisioning.microchip import MicrochipIpeProvisioner
from pytest_mes_core.provisioning.base import (
    ProvisioningError,
    ImageVerificationError,
    SiliconLockError,
)
from pytest_mes_core.utils.process import ProcessExecutionError, ProcessTimeoutError


@pytest.fixture
def mock_pickit():
    adapter = MagicMock(spec=HostPickitAdapter)
    adapter.tool_serial = "BUR123456789"
    adapter.tool_type = "PK4"
    return adapter


@patch("pytest_mes_core.provisioning.microchip.LiveProcess")
def test_microchip_provisioner_success(mock_live_process, tmp_path, mock_pickit):
    ipecmd_file = tmp_path / "ipecmd.sh"
    ipecmd_file.touch()
    fw_file = tmp_path / "firmware.hex"
    fw_file.touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="Program Succeeded.\nVerification successful.\nOperation Succeeded",
        duration_s=15.0,
    )
    mock_live_process.return_value = mock_proc

    provisioner = MicrochipIpeProvisioner(
        pickit_adapter=mock_pickit,
        ipecmd_path=str(ipecmd_file),
        device="18F57Q43",
        timeout_s=60,
        extra_flags=["-W3.3"],
        retries=1,
    )

    result = provisioner.provision(fw_file)
    assert result is True

    # Verify command assembly and cwd
    called_cmd = mock_live_process.call_args[0][0]
    called_cwd = mock_live_process.call_args[1].get("cwd")

    assert str(ipecmd_file.resolve()) in called_cmd
    assert "-P18F57Q43" in called_cmd
    assert "-TSBUR123456789" in called_cmd
    assert f"-F{fw_file.resolve()}" in called_cmd
    assert "-E" in called_cmd
    assert "-M" in called_cmd
    assert "-Y" in called_cmd
    assert "-W3.3" in called_cmd
    assert called_cwd == ipecmd_file.parent


@patch("pytest_mes_core.provisioning.microchip.LiveProcess")
def test_microchip_provisioner_omits_oh_flag(mock_live_process, tmp_path, mock_pickit):
    ipecmd_file = tmp_path / "ipecmd.sh"
    ipecmd_file.touch()
    fw_file = tmp_path / "firmware.hex"
    fw_file.touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="Program Succeeded.\nVerification successful.",
        duration_s=10.0,
    )
    mock_live_process.return_value = mock_proc

    provisioner = MicrochipIpeProvisioner(
        pickit_adapter=mock_pickit,
        ipecmd_path=str(ipecmd_file),
        device="18F57Q43",
        extra_flags=["-W3.3", "-OH"],
        retries=0,
    )

    provisioner.provision(fw_file)
    called_cmd = mock_live_process.call_args[0][0]
    assert "-OH" not in called_cmd
    assert "-E" in called_cmd


@patch("pytest_mes_core.provisioning.microchip.LiveProcess")
def test_microchip_provisioner_sanitizes_tool_type(mock_live_process, tmp_path, mock_pickit):
    ipecmd_file = tmp_path / "ipecmd.sh"
    ipecmd_file.touch()
    fw_file = tmp_path / "firmware.hex"
    fw_file.touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="Program Succeeded.",
        duration_s=10.0,
    )
    mock_live_process.return_value = mock_proc

    provisioner = MicrochipIpeProvisioner(
        pickit_adapter=mock_pickit,
        ipecmd_path=str(ipecmd_file),
        device="18F57Q43",
        extra_flags=["-TPPK3"],  # Mismatched flag when adapter is PK4
        retries=0,
    )

    provisioner.provision(fw_file)
    called_cmd = mock_live_process.call_args[0][0]
    assert "-TPPK4" in called_cmd
    assert "-TPPK3" not in called_cmd


@patch("pytest_mes_core.provisioning.microchip.LiveProcess")
def test_microchip_provisioner_retry_on_verification_failure(mock_live_process, tmp_path, mock_pickit):
    ipecmd_file = tmp_path / "ipecmd.sh"
    ipecmd_file.touch()
    fw_file = tmp_path / "firmware.hex"
    fw_file.touch()

    # First attempt fails verification, second succeeds
    fail_proc = MagicMock()
    fail_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="Verify failed at 0x2000",
        duration_s=10.0,
        export_log=lambda path: "/tmp/log.txt",
    )

    succ_proc = MagicMock()
    succ_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="Program Succeeded.\nVerification successful.",
        duration_s=10.0,
    )

    mock_live_process.side_effect = [fail_proc, succ_proc]

    provisioner = MicrochipIpeProvisioner(
        pickit_adapter=mock_pickit,
        ipecmd_path=str(ipecmd_file),
        device="18F57Q43",
        retries=1,
    )

    result = provisioner.provision(fw_file)
    assert result is True
    assert mock_live_process.call_count == 2


@patch("pytest_mes_core.provisioning.microchip.LiveProcess")
def test_microchip_provisioner_fails_immediately_on_silicon_lock(mock_live_process, tmp_path, mock_pickit):
    ipecmd_file = tmp_path / "ipecmd.sh"
    ipecmd_file.touch()
    fw_file = tmp_path / "firmware.hex"
    fw_file.touch()

    fail_proc = MagicMock()
    fail_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="Device is code protected",
        duration_s=5.0,
        export_log=lambda path: "/tmp/log.txt",
    )
    mock_live_process.return_value = fail_proc

    provisioner = MicrochipIpeProvisioner(
        pickit_adapter=mock_pickit,
        ipecmd_path=str(ipecmd_file),
        device="18F57Q43",
        retries=2,
    )

    with pytest.raises(SiliconLockError):
        provisioner.provision(fw_file)

    # Should not retry locked silicon
    assert mock_live_process.call_count == 1
