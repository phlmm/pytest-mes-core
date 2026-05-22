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


@patch("pytest_mes_core.provisioning.tezi_uuu.LiveProcess")
@patch.object(UuuTeziProvisioner, "_is_device_in_recovery")
def test_tezi_provision_disconnects_serial_client_on_success(mock_is_device, mock_live_process, tmp_path):
    mock_is_device.return_value = True
    payload_dir = tmp_path / "tezi"
    payload_dir.mkdir()
    (payload_dir / "uuu.auto").touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="100%] Done",
        duration_s=1.0
    )
    mock_live_process.return_value = mock_proc

    from pytest_mes_core.events import PromptDetected, BootDataReceived
    events = [
        PromptDetected(prompt_type="tezi_shell_hash", elapsed_s=0.1),
        BootDataReceived(line="__MES_TX_OK__", elapsed_s=0.2),
        BootDataReceived(line="__MES_MEDIA_OK__", elapsed_s=0.3),
        PromptDetected(prompt_type="post_install_login", elapsed_s=0.4)
    ]

    with patch("pytest_mes_core.state_machine.UartEventStream") as mock_stream_cls:
        mock_stream = MagicMock()
        mock_stream.open.return_value = events
        mock_stream_cls.return_value = mock_stream

        serial_client = MagicMock()
        serial_client.is_connected = False

        provisioner = UuuTeziProvisioner(usb_path="1:1")
        res = provisioner.provision(image_path=payload_dir, serial_client=serial_client)

        assert res is True
        serial_client.connect.assert_called_once()
        serial_client.disconnect.assert_called_once()


@patch("pytest_mes_core.provisioning.tezi_uuu.LiveProcess")
@patch.object(UuuTeziProvisioner, "_is_device_in_recovery")
def test_tezi_provision_disconnects_serial_client_on_failure(mock_is_device, mock_live_process, tmp_path):
    mock_is_device.return_value = True
    payload_dir = tmp_path / "tezi"
    payload_dir.mkdir()
    (payload_dir / "uuu.auto").touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="100%] Done",
        duration_s=1.0
    )
    mock_live_process.return_value = mock_proc

    with patch("pytest_mes_core.state_machine.UartEventStream") as mock_stream_cls:
        mock_stream = MagicMock()
        mock_stream.open.side_effect = RuntimeError("UART read failed")
        mock_stream_cls.return_value = mock_stream

        serial_client = MagicMock()
        serial_client.is_connected = True

        provisioner = UuuTeziProvisioner(usb_path="1:1")
        with pytest.raises(RuntimeError, match="UART read failed"):
            provisioner.provision(image_path=payload_dir, serial_client=serial_client)

        # Even with the error in the event loop, disconnect should be called in finally block
        serial_client.disconnect.assert_called_once()


@patch("pytest_mes_core.provisioning.tezi_uuu.LiveProcess")
@patch.object(UuuTeziProvisioner, "_is_device_in_recovery")
def test_tezi_provision_media_check_ok(mock_is_device, mock_live_process, tmp_path):
    mock_is_device.return_value = True
    payload_dir = tmp_path / "tezi"
    payload_dir.mkdir()
    (payload_dir / "uuu.auto").touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="100%] Done",
        duration_s=1.0
    )
    mock_live_process.return_value = mock_proc

    from pytest_mes_core.events import PromptDetected, BootDataReceived
    events = [
        PromptDetected(prompt_type="tezi_shell_hash", elapsed_s=0.1),
        BootDataReceived(line="__MES_TX_OK__", elapsed_s=0.2),
        BootDataReceived(line="__MES_MEDIA_OK__", elapsed_s=0.3),
        PromptDetected(prompt_type="post_install_login", elapsed_s=0.4)
    ]

    with patch("pytest_mes_core.state_machine.UartEventStream") as mock_stream_cls:
        mock_stream = MagicMock()
        mock_stream.open.return_value = events
        mock_stream_cls.return_value = mock_stream

        serial_client = MagicMock()
        serial_client.is_connected = False

        provisioner = UuuTeziProvisioner(usb_path="1:1")
        res = provisioner.provision(image_path=payload_dir, serial_client=serial_client)

        assert res is True
        # Check raw_writes on serial_client
        write_calls = b"".join([call[0][0] for call in serial_client.raw_write.call_args_list])
        assert b"echo __MES_TX_OK__" in write_calls
        assert b"media_found=" in write_calls
        assert b"tail -n +1" in write_calls


@patch("pytest_mes_core.provisioning.tezi_uuu.LiveProcess")
@patch.object(UuuTeziProvisioner, "_is_device_in_recovery")
def test_tezi_provision_media_check_missing_raises_error(mock_is_device, mock_live_process, tmp_path):
    mock_is_device.return_value = True
    payload_dir = tmp_path / "tezi"
    payload_dir.mkdir()
    (payload_dir / "uuu.auto").touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="100%] Done",
        duration_s=1.0
    )
    mock_live_process.return_value = mock_proc

    from pytest_mes_core.events import PromptDetected, BootDataReceived
    events = [
        PromptDetected(prompt_type="tezi_shell_hash", elapsed_s=0.1),
        BootDataReceived(line="__MES_TX_OK__", elapsed_s=0.2),
        BootDataReceived(line="__MES_MEDIA_MISSING__", elapsed_s=0.3)
    ]

    with patch("pytest_mes_core.state_machine.UartEventStream") as mock_stream_cls:
        mock_stream = MagicMock()
        mock_stream.open.return_value = events
        mock_stream_cls.return_value = mock_stream

        serial_client = MagicMock()
        serial_client.is_connected = False

        provisioner = UuuTeziProvisioner(usb_path="1:1")
        with pytest.raises(ProvisioningError, match="No removable media detected"):
            provisioner.provision(image_path=payload_dir, serial_client=serial_client)


@patch("pytest_mes_core.provisioning.tezi_uuu.LiveProcess")
@patch.object(UuuTeziProvisioner, "_is_device_in_recovery")
@patch("pytest_mes_core.provisioning.tezi_uuu.time.perf_counter")
def test_tezi_provision_media_check_timeout_raises_error(mock_perf, mock_is_device, mock_live_process, tmp_path):
    mock_is_device.return_value = True
    payload_dir = tmp_path / "tezi"
    payload_dir.mkdir()
    (payload_dir / "uuu.auto").touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="100%] Done",
        duration_s=1.0
    )
    mock_live_process.return_value = mock_proc

    # Use side effect function to return timeout values on subsequent calls
    call_count = 0
    def fake_perf():
        nonlocal call_count
        call_count += 1
        if call_count >= 8:
            return 100.0
        return 1.0
    mock_perf.side_effect = fake_perf

    from pytest_mes_core.events import PromptDetected, BootDataReceived
    events = [
        PromptDetected(prompt_type="tezi_shell_hash", elapsed_s=0.1),
        BootDataReceived(line="__MES_TX_OK__", elapsed_s=0.2),
        BootDataReceived(line="random line", elapsed_s=0.3)
    ]

    with patch("pytest_mes_core.state_machine.UartEventStream") as mock_stream_cls:
        mock_stream = MagicMock()
        mock_stream.open.return_value = events
        mock_stream_cls.return_value = mock_stream

        serial_client = MagicMock()
        serial_client.is_connected = False

        provisioner = UuuTeziProvisioner(usb_path="1:1")
        with pytest.raises(ProvisioningError, match="Timeout waiting for removable media check response"):
            provisioner.provision(image_path=payload_dir, serial_client=serial_client)


@patch("pytest_mes_core.provisioning.tezi_uuu.LiveProcess")
@patch.object(UuuTeziProvisioner, "_is_device_in_recovery")
def test_tezi_provision_media_check_ignores_command_echo(mock_is_device, mock_live_process, tmp_path):
    mock_is_device.return_value = True
    payload_dir = tmp_path / "tezi"
    payload_dir.mkdir()
    (payload_dir / "uuu.auto").touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="100%] Done",
        duration_s=1.0
    )
    mock_live_process.return_value = mock_proc

    from pytest_mes_core.events import PromptDetected, BootDataReceived
    events = [
        PromptDetected(prompt_type="tezi_shell_hash", elapsed_s=0.1),
        BootDataReceived(line="__MES_TX_OK__", elapsed_s=0.2),
        # Echo of the check command containing the split version of the token
        BootDataReceived(
            line='attempt=1; media_found=0; while [ $attempt -le 10 ]; do emmc_base=""; for b in /sys/block/*boot0; do if [ -e "$b" ]; then b_name="${b##*/}"; emmc_base="${b_name%boot0}"; break; fi; done; for d in /sys/block/sd* /sys/block/mmcblk*; do if [ -d "$d" ]; then d_name="${d##*/}"; if [ -n "$emmc_base" ]; then case "$d_name" in "$emmc_base"*) continue;; esac; fi; if [ -f "$d/removable" ] && [ "$(cat $d/removable)" = "1" ]; then media_found=1; break; fi; fi; done; if [ "$media_found" = "1" ]; then break; fi; attempt=$((attempt+1)); sleep 1; done; if [ "$media_found" = "1" ]; then echo "__MES_ME""DIA_OK__"; else echo "__MES_MEDIA""_MISSING__"; fi',
            elapsed_s=0.3
        ),
        # The actual output printed by the running script on the DUT (which indicates missing)
        BootDataReceived(line="__MES_MEDIA_MISSING__", elapsed_s=0.4)
    ]

    with patch("pytest_mes_core.state_machine.UartEventStream") as mock_stream_cls:
        mock_stream = MagicMock()
        mock_stream.open.return_value = events
        mock_stream_cls.return_value = mock_stream

        serial_client = MagicMock()
        serial_client.is_connected = False

        provisioner = UuuTeziProvisioner(usb_path="1:1")
        # Should raise ProvisioningError because it ignores the echo and correctly acts on the __MES_MEDIA_MISSING__ output
        with pytest.raises(ProvisioningError, match="No removable media detected"):
            provisioner.provision(image_path=payload_dir, serial_client=serial_client)


@patch("pytest_mes_core.provisioning.tezi_uuu.LiveProcess")
@patch.object(UuuTeziProvisioner, "_is_device_in_recovery")
def test_tezi_provision_media_check_ignores_command_echo_success(mock_is_device, mock_live_process, tmp_path):
    mock_is_device.return_value = True
    payload_dir = tmp_path / "tezi"
    payload_dir.mkdir()
    (payload_dir / "uuu.auto").touch()

    mock_proc = MagicMock()
    mock_proc.execute.return_value = MagicMock(
        returncode=0,
        stdout="100%] Done",
        duration_s=1.0
    )
    mock_live_process.return_value = mock_proc

    from pytest_mes_core.events import PromptDetected, BootDataReceived
    events = [
        PromptDetected(prompt_type="tezi_shell_hash", elapsed_s=0.1),
        BootDataReceived(line="__MES_TX_OK__", elapsed_s=0.2),
        # Echo of the check command containing the split version of the token
        BootDataReceived(
            line='attempt=1; media_found=0; while [ $attempt -le 10 ]; do emmc_base=""; for b in /sys/block/*boot0; do if [ -e "$b" ]; then b_name="${b##*/}"; emmc_base="${b_name%boot0}"; break; fi; done; for d in /sys/block/sd* /sys/block/mmcblk*; do if [ -d "$d" ]; then d_name="${d##*/}"; if [ -n "$emmc_base" ]; then case "$d_name" in "$emmc_base"*) continue;; esac; fi; if [ -f "$d/removable" ] && [ "$(cat $d/removable)" = "1" ]; then media_found=1; break; fi; fi; done; if [ "$media_found" = "1" ]; then break; fi; attempt=$((attempt+1)); sleep 1; done; if [ "$media_found" = "1" ]; then echo "__MES_ME""DIA_OK__"; else echo "__MES_MEDIA""_MISSING__"; fi',
            elapsed_s=0.3
        ),
        # The actual output printed by the running script on the DUT (which indicates success)
        BootDataReceived(line="__MES_MEDIA_OK__", elapsed_s=0.4),
        PromptDetected(prompt_type="post_install_login", elapsed_s=0.5)
    ]

    with patch("pytest_mes_core.state_machine.UartEventStream") as mock_stream_cls:
        mock_stream = MagicMock()
        mock_stream.open.return_value = events
        mock_stream_cls.return_value = mock_stream

        serial_client = MagicMock()
        serial_client.is_connected = False

        provisioner = UuuTeziProvisioner(usb_path="1:1")
        res = provisioner.provision(image_path=payload_dir, serial_client=serial_client)
        assert res is True


