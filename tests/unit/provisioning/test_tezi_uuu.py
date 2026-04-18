import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from pytest_mes_core.provisioning.tezi_uuu import UuuTeziProvisioner
from pytest_mes_core.provisioning.base import ProvisioningError

@patch("pytest_mes_core.provisioning.tezi_uuu.subprocess.run")
def test_tezi_usb_topology_binding_respects_usb_path(mock_run):
    provisioner = UuuTeziProvisioner(wait_for_recovery_s=1, usb_path="1:2.4")

    # Mock uuu -lsusb returning Jig A and Jig B
    mock_run.return_value = MagicMock(stdout="1:2.4 NXP 1fc9\n2:1.1 NXP 1fc9", stderr="", returncode=0)

    # It should return True because 1:2.4 is in the output
    assert provisioner._is_device_in_recovery() is True

    # But what if only Jig B is in recovery?
    mock_run.return_value = MagicMock(stdout="2:1.1 NXP 1fc9", stderr="", returncode=0)
    assert provisioner._is_device_in_recovery() is False

    # Ensure it only ran uuu, not the generic lsusb!
    mock_run.assert_called_with(["uuu", "-lsusb"], capture_output=True, text=True, timeout=5)

@patch("pytest_mes_core.provisioning.tezi_uuu.subprocess.run")
def test_tezi_native_lsusb_fallback(mock_run):
    # No usb_path defined
    provisioner = UuuTeziProvisioner(wait_for_recovery_s=1, usb_path=None)

    # Mock native lsusb
    mock_run.return_value = MagicMock(stdout="Bus 001 Device 002: ID 1fc9:012b NXP Semiconductors", stderr="", returncode=0)

    assert provisioner._is_device_in_recovery() is True
    mock_run.assert_called_with(["lsusb"], capture_output=True, text=True, timeout=5)

@patch("pytest_mes_core.provisioning.tezi_uuu.LiveProcess")
@patch.object(UuuTeziProvisioner, "_is_device_in_recovery")
def test_tezi_provision_successful(mock_is_device, mock_live_process, tmp_path):
    mock_is_device.return_value = True

    # Setup dummy TEZI payload
    payload_dir = tmp_path / "tezi"
    payload_dir.mkdir()
    (payload_dir / "uuu.auto").touch()

    # Mock LiveProcess
    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="100%] Done",
        duration_s=1.0
    )
    mock_live_process.return_value = mock_proc

    provisioner = UuuTeziProvisioner(usb_path="1:1")
    
    # Should not raise any exceptions
    res = provisioner.provision(image_path=payload_dir)
    
    assert res is True
