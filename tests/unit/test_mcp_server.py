"""Unit tests for pytest-mes-core MCP server and station manager."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pytest_mes_core.mcp.server import (
    board_expect,
    board_file_exists,
    board_raw_write,
    board_read_file,
    board_run,
    board_run_serial,
    board_run_ssh,
    board_write_file,
    device_tree_read,
    fsm_boot_to_bootloader,
    fsm_boot_to_os,
    fsm_boot_to_recovery,
    fsm_energize,
    fsm_mark_dirty,
    fsm_power_off,
    fsm_state,
    i2c_read_byte,
    i2c_write_byte,
    kernel_config_check,
    kernel_dmesg,
    manager,
    psu_disable_output,
    psu_enable_output,
    psu_measure,
    psu_set_current_limit,
    psu_set_voltage,
    resource_station_state,
    resource_stations_list,
    station_connect,
    station_disconnect,
    station_list,
    station_status,
    sysfs_read,
    sysfs_write,
    uboot_env_get,
    uboot_env_set,
    uboot_save_env,
    validate_i2c_bus,
    watchdog_status,
)
from pytest_mes_core.mcp.station_manager import StationManager
from pytest_mes_core.transports.base import CommandResult
from tests.mocks.virtual_transport import MockTransport

# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_manager():
    """Ensure the global server station manager is clean between tests."""
    manager._stations.clear()
    yield
    manager._stations.clear()


@pytest.fixture
def minimal_toml_path(tmp_path: Path) -> Path:
    """Creates a valid minimal station_env.toml file."""
    content = """
[station_meta]
facility = "factory-floor-1"
jig_id = "jig-001"

[telemetry]
enabled = false
"""
    p = tmp_path / "station_env.toml"
    p.write_text(content)
    return p


# ── StationManager Tests ─────────────────────────────────────────

def test_station_manager_empty():
    mgr = StationManager()
    assert mgr.list_stations() == []
    assert "error" in mgr.disconnect("non_existent")

    with pytest.raises(ValueError, match="not connected"):
        mgr.get_station("non_existent")


def test_station_manager_connect_missing_file():
    mgr = StationManager()
    res = mgr.connect("station1", Path("/tmp/does_not_exist_station.toml"))
    assert "error" in res
    assert "not found" in res["error"]


def test_station_manager_connect_mock_transport(minimal_toml_path: Path):
    mgr = StationManager()
    mock_t = MockTransport()
    res = mgr.connect("station_mock", minimal_toml_path, mock_transport=mock_t)

    assert "error" not in res
    assert res["station_id"] == "station_mock"
    assert res["transport_type"] == "MockTransport"

    # Already connected error
    res_dup = mgr.connect("station_mock", minimal_toml_path)
    assert "error" in res_dup

    # List
    stations = mgr.list_stations()
    assert len(stations) == 1
    assert stations[0]["station_id"] == "station_mock"

    # Status
    status = mgr.get_status("station_mock")
    assert status["station_id"] == "station_mock"

    # Disconnect
    disc = mgr.disconnect("station_mock")
    assert disc.get("success") is True
    assert mgr.list_stations() == []


def test_station_manager_discover():
    mgr = StationManager()
    disc = mgr.discover()
    assert "serial_ports" in disc
    assert "recovery_devices" in disc
    assert isinstance(disc["serial_ports"], list)
    assert isinstance(disc["recovery_devices"], list)


# ── MCP Server Tool Tests ────────────────────────────────────────

def test_server_station_lifecycle_tools(minimal_toml_path: Path):
    # Connect
    res = station_connect("st1", str(minimal_toml_path))
    # It might fail with "No enabled serial or SSH transport" because minimal TOML has none
    assert "error" in res

    # Manually populate station with mock
    mock_t = MockTransport()
    manager._stations["st1"] = {
        "config": MagicMock(),
        "config_path": minimal_toml_path,
        "fsm": None,
        "transport": mock_t,
        "serial": None,
        "ssh": None,
        "psu": None,
        "watchdog": None,
    }

    assert len(station_list()) == 1
    status = station_status("st1")
    assert status["station_id"] == "st1"

    disc = station_disconnect("st1")
    assert disc.get("success") is True


def test_server_board_run_commands():
    mock_t = MockTransport()
    mock_t.register_mock_response("echo hello", "hello")
    mock_t.register_mock_response("cat /etc/version", "1.0.0")
    mock_t.register_mock_response("cat << 'MES_EOF'", "")
    mock_t.register_mock_response("test -e /etc/hosts", "")
    manager._stations["st_cmd"] = {
        "config": MagicMock(),
        "transport": mock_t,
        "serial": None,
        "ssh": None,
    }

    res = board_run("st_cmd", "echo hello")
    assert res["command"] == "echo hello"
    assert res["ok"] is True
    assert res["exit_code"] == 0

    read_res = board_read_file("st_cmd", "/etc/version")
    assert read_res["path"] == "/etc/version"
    assert read_res["ok"] is True

    write_res = board_write_file("st_cmd", "/tmp/test.txt", "my payload")
    assert write_res["path"] == "/tmp/test.txt"
    assert write_res["success"] is True

    exists_res = board_file_exists("st_cmd", "/etc/hosts")
    assert exists_res["path"] == "/etc/hosts"
    assert exists_res["exists"] is True


def test_server_transport_isolated_runs():
    mock_ssh = MagicMock()
    mock_ssh.is_connected = True
    mock_ssh.safe_run.return_value = CommandResult(
        command="ssh cmd", stdout="ssh out", stderr="", exited=0, ok=True, duration_s=0.1
    )

    mock_serial = MagicMock()
    mock_serial.is_connected = True
    mock_serial.safe_run.return_value = CommandResult(
        command="serial cmd", stdout="serial out", stderr="", exited=0, ok=True, duration_s=0.2
    )
    mock_serial.expect.return_value = True

    manager._stations["st_iso"] = {
        "config": MagicMock(),
        "transport": mock_ssh,
        "ssh": mock_ssh,
        "serial": mock_serial,
    }

    # SSH run
    res_ssh = board_run_ssh("st_iso", "ssh cmd")
    assert res_ssh["stdout"] == "ssh out"

    # Serial run
    res_ser = board_run_serial("st_iso", "serial cmd")
    assert res_ser["stdout"] == "serial out"

    # Expect
    exp = board_expect("st_iso", "prompt#")
    assert exp["matched"] is True

    # Raw write
    raw = board_raw_write("st_iso", "reboot\n")
    assert raw["success"] is True
    assert mock_serial.raw_write.called


def test_server_fsm_tools():
    mock_fsm = MagicMock()
    mock_fsm.state.name = "OS_USERLAND"
    mock_fsm.verify_heartbeat.return_value = True

    manager._stations["st_fsm"] = {
        "config": MagicMock(),
        "fsm": mock_fsm,
        "transport": MagicMock(),
    }

    st = fsm_state("st_fsm")
    assert st["state"] == "OS_USERLAND"
    assert st["is_alive"] is True

    assert fsm_boot_to_os("st_fsm")["success"] is True
    assert mock_fsm.boot_to_os.called

    assert fsm_boot_to_bootloader("st_fsm")["success"] is True
    assert mock_fsm.boot_to_bootloader.called

    assert fsm_boot_to_recovery("st_fsm")["success"] is True
    assert mock_fsm.boot_to_recovery.called

    assert fsm_power_off("st_fsm")["success"] is True
    assert mock_fsm.power_off.called

    assert fsm_energize("st_fsm")["success"] is True
    assert mock_fsm.energize.called

    assert fsm_mark_dirty("st_fsm")["state"] == "DIRTY"
    assert mock_fsm.mark_dirty.called


def test_server_psu_tools():
    mock_psu = MagicMock()
    mock_psu.measure_voltage.return_value = 12.0
    mock_psu.measure_current.return_value = 1.25

    manager._stations["st_psu"] = {
        "config": MagicMock(),
        "psu": mock_psu,
        "transport": MagicMock(),
    }

    assert psu_enable_output("st_psu")["success"] is True
    assert mock_psu.enable_output.called

    assert psu_disable_output("st_psu")["success"] is True
    assert mock_psu.disable_output.called

    assert psu_set_voltage("st_psu", 12.5)["voltage_v"] == 12.5
    mock_psu.set_voltage.assert_called_with(12.5)

    assert psu_set_current_limit("st_psu", 2.0)["current_a"] == 2.0

    meas = psu_measure("st_psu")
    assert meas["voltage_v"] == 12.0
    assert meas["current_a"] == 1.25


def test_server_i2c_tools():
    mock_t = MockTransport()
    manager._stations["st_i2c"] = {
        "config": MagicMock(),
        "transport": mock_t,
    }

    # Detect
    det = validate_i2c_bus("st_i2c", bus=1)
    assert det["bus"] == 1
    assert isinstance(det["devices"], list)

    # Read byte
    rb = i2c_read_byte("st_i2c", bus=1, chip_addr="0x42", reg_addr="0x00")
    assert rb["bus"] == 1
    assert "value" in rb

    # Write byte
    wb = i2c_write_byte("st_i2c", bus=1, chip_addr="0x42", reg_addr="0x00", value="0xFF")
    assert wb["success"] is True


def test_server_sysfs_and_kernel_tools():
    mock_t = MockTransport()
    mock_t.register_mock_response("echo '1' > /sys/class/leds/status/brightness", "")
    mock_t.register_mock_response("zcat /proc/config.gz | grep -w 'CONFIG_CAN'", "CONFIG_CAN=y")
    manager._stations["st_kern"] = {
        "config": MagicMock(),
        "transport": mock_t,
    }

    # Sysfs read/write
    sr = sysfs_read("st_kern", "/sys/class/thermal/thermal_zone0/temp")
    assert sr["value"] == "45000"
    assert sr["ok"] is True

    sw = sysfs_write("st_kern", "/sys/class/leds/status/brightness", "1")
    assert sw["success"] is True

    # Kernel dmesg
    kd = kernel_dmesg("st_kern", filter_pattern="error", lines=10)
    assert "output" in kd

    # Device tree read
    dt = device_tree_read("st_kern", "serial-number")
    assert dt["value"] == "MOCK-TORADEX-9999"

    # Config check
    cfg = kernel_config_check("st_kern", "CONFIG_CAN")
    assert "present" in cfg


def test_server_watchdog_tool():
    mock_wd = MagicMock()
    mock_wd.is_panicked.return_value = False
    mock_wd._muted = False

    manager._stations["st_wd"] = {
        "config": MagicMock(),
        "watchdog": mock_wd,
        "transport": MagicMock(),
    }

    status = watchdog_status("st_wd")
    assert status["active"] is True
    assert status["is_panicked"] is False
    assert status["is_muted"] is False


def test_server_uboot_tools():
    mock_serial = MagicMock()
    mock_serial.safe_run.return_value = CommandResult(
        command="printenv bootcmd",
        stdout="bootcmd=run distro_bootcmd",
        stderr="",
        exited=0,
        ok=True,
        duration_s=0.1,
    )

    manager._stations["st_uboot"] = {
        "config": MagicMock(),
        "serial": mock_serial,
        "transport": mock_serial,
    }

    var = uboot_env_get("st_uboot", "bootcmd")
    assert var["value"] == "run distro_bootcmd"

    set_res = uboot_env_set("st_uboot", "bootcmd", "run mmcboot")
    assert set_res["success"] is True

    save_res = uboot_save_env("st_uboot")
    assert save_res["success"] is True


def test_server_resources():
    mock_fsm = MagicMock()
    mock_fsm.state.name = "ENERGIZED"
    mock_t = MockTransport()

    manager._stations["st_res"] = {
        "config": MagicMock(),
        "fsm": mock_fsm,
        "transport": mock_t,
    }

    # stations list resource
    raw_list = resource_stations_list()
    data = json.loads(raw_list)
    assert len(data) == 1
    assert data[0]["station_id"] == "st_res"

    # station state resource
    raw_state = resource_station_state("st_res")
    state_data = json.loads(raw_state)
    assert state_data["state"] == "ENERGIZED"


def test_server_sd_mux_and_flash_tezi(monkeypatch):
    from pytest_mes_core.mcp.server import flash_tezi, sd_mux_set_mode

    mock_mux = MagicMock()
    mock_mux.device_path = "/dev/usb-sd-mux/id-00048.00717"

    mock_cfg = MagicMock()
    mock_tezi_cfg = MagicMock()
    mock_tezi_cfg.usb_recovery_timeout_s = 60
    mock_tezi_cfg.flash_timeout_s = 300
    mock_tezi_cfg.tezi_folder_path = "/fake/tezi"
    mock_cfg.tezi_provisioning = {"os_ram_loader": mock_tezi_cfg}

    manager._stations["st_flash"] = {
        "config": mock_cfg,
        "sd_mux": mock_mux,
        "serial": MagicMock(),
        "fsm": MagicMock(),
    }

    # Test SD mux switching
    mux_res = sd_mux_set_mode("st_flash", "dut")
    assert mux_res["status"] == "ok"
    assert mux_res["mode"] == "dut"
    mock_mux._set_mux_state.assert_called_with("dut")

    # Mock UuuTeziProvisioner
    mock_prov_instance = MagicMock()
    mock_prov_instance.provision.return_value = True
    mock_prov_cls = MagicMock(return_value=mock_prov_instance)
    monkeypatch.setattr("pytest_mes_core.provisioning.tezi_uuu.UuuTeziProvisioner", mock_prov_cls)

    flash_res = flash_tezi("st_flash", image_path="/fake/tezi")
    assert flash_res["success"] is True
    mock_prov_instance.provision.assert_called_once()
