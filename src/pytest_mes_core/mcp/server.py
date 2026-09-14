"""MCP server for pytest-mes-core — agent access to embedded test stations."""

from __future__ import annotations

import json
from pathlib import Path

import structlog

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from pytest_mes_core.mcp.station_manager import StationManager

logger = structlog.get_logger("mes_core.mcp.server")

mcp = MCPServer(
    "pytest-mes-core",
    description=(
        "Manufacturing execution & hardware validation engine for embedded systems. "
        "Connect to test stations with embedded Linux boards and MCUs, "
        "drive state machines, execute commands over SSH/UART failover, "
        "run hardware protocol validators, control power supplies, "
        "flash firmware, and collect factory telemetry."
    ),
)

manager = StationManager()


# ── Station Lifecycle ────────────────────────────────────────────

@mcp.tool()
def station_connect(station_id: str, config_path: str) -> dict:
    """Connect to a test station by loading its configuration TOML.

    Args:
        station_id: Unique identifier for this station session.
        config_path: Path to station_env.toml file.
    """
    return manager.connect(station_id, Path(config_path))


@mcp.tool()
def station_disconnect(station_id: str) -> dict:
    """Disconnect from a station, safely power off, and release hardware resources.

    Args:
        station_id: Station identifier.
    """
    return manager.disconnect(station_id)


@mcp.tool()
def station_list() -> list[dict]:
    """List all connected stations with their current FSM state."""
    return manager.list_stations()


@mcp.tool()
def station_status(station_id: str) -> dict:
    """Get detailed station status: FSM state, transport status, PSU readings.

    Args:
        station_id: Station identifier.
    """
    return manager.get_status(station_id)


@mcp.tool()
def station_discover() -> dict:
    """Auto-discover connected hardware (serial TTY ports and USB recovery devices)."""
    return manager.discover()


# ── State Machine Control ────────────────────────────────────────

@mcp.tool()
def fsm_state(station_id: str) -> dict:
    """Get the current physical state of the board.

    Args:
        station_id: Station identifier.
    """
    station = manager.get_station(station_id)
    fsm = station.get("fsm")
    if fsm is None:
        return {"state": "NO_FSM", "error": "No State Machine configured for this station"}
    state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)
    return {
        "state": state_name,
        "is_alive": fsm.verify_heartbeat() if hasattr(fsm, "verify_heartbeat") else None,
    }


@mcp.tool()
def fsm_boot_to_os(station_id: str) -> dict:
    """Boot the board from any state to Linux OS_USERLAND prompt.

    Args:
        station_id: Station identifier.
    """
    station = manager.get_station(station_id)
    fsm = station.get("fsm")
    if fsm is None:
        return {"success": False, "error": "No State Machine configured"}
    fsm.boot_to_os()
    state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)
    return {"success": True, "state": state_name}


@mcp.tool()
def fsm_boot_to_bootloader(station_id: str) -> dict:
    """Boot to U-Boot prompt, intercepting autoboot.

    Args:
        station_id: Station identifier.
    """
    station = manager.get_station(station_id)
    fsm = station.get("fsm")
    if fsm is None:
        return {"success": False, "error": "No State Machine configured"}
    fsm.boot_to_bootloader()
    state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)
    return {"success": True, "state": state_name}


@mcp.tool()
def fsm_boot_to_recovery(station_id: str) -> dict:
    """Enter SoC recovery/SDP mode for USB flashing.

    Args:
        station_id: Station identifier.
    """
    station = manager.get_station(station_id)
    fsm = station.get("fsm")
    if fsm is None:
        return {"success": False, "error": "No State Machine configured"}
    fsm.boot_to_recovery()
    state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)
    return {"success": True, "state": state_name}


@mcp.tool()
def fsm_power_off(station_id: str) -> dict:
    """Safely drop power to the DUT.

    Args:
        station_id: Station identifier.
    """
    station = manager.get_station(station_id)
    fsm = station.get("fsm")
    if fsm is None:
        # Fallback to direct PSU control if no FSM
        psu = station.get("psu")
        if psu:
            psu.disable_output()
            return {"success": True, "state": "POWER_OFF_DIRECT"}
        return {"success": False, "error": "Neither FSM nor PSU configured"}
    fsm.power_off()
    state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)
    return {"success": True, "state": state_name}


@mcp.tool()
def fsm_energize(station_id: str) -> dict:
    """Apply power to the DUT.

    Args:
        station_id: Station identifier.
    """
    station = manager.get_station(station_id)
    fsm = station.get("fsm")
    if fsm is None:
        psu = station.get("psu")
        if psu:
            psu.enable_output()
            return {"success": True, "state": "ENERGIZED_DIRECT"}
        return {"success": False, "error": "Neither FSM nor PSU configured"}
    fsm.energize()
    state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)
    return {"success": True, "state": state_name}


@mcp.tool()
def fsm_mark_dirty(station_id: str) -> dict:
    """Mark state as DIRTY, forcing cold reboot on next transition.

    Args:
        station_id: Station identifier.
    """
    station = manager.get_station(station_id)
    fsm = station.get("fsm")
    if fsm is None:
        return {"success": False, "error": "No State Machine configured"}
    fsm.mark_dirty()
    return {"success": True, "state": "DIRTY"}


# ── Command Execution ────────────────────────────────────────────

@mcp.tool()
def board_run(
    station_id: str,
    command: str,
    timeout: float = 30.0,
    check_exit_code: bool = True,
) -> dict:
    """Execute a shell command on the board via failover transport (SSH with UART fallback).

    Args:
        station_id: Station identifier.
        command: Shell command to execute.
        timeout: Maximum seconds to wait.
        check_exit_code: If true, marks ok=False on non-zero exit code.
    """
    station = manager.get_station(station_id)
    transport = station["transport"]
    result = transport.safe_run(command, timeout_s=timeout, check_exit_code=check_exit_code)
    return {
        "command": result.command,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exited,
        "ok": result.ok,
        "duration_s": result.duration_s,
    }


@mcp.tool()
def board_run_ssh(
    station_id: str,
    command: str,
    timeout: float = 30.0,
    check_exit_code: bool = True,
) -> dict:
    """Execute a shell command strictly over the SSH transport.

    Args:
        station_id: Station identifier.
        command: Shell command to execute.
        timeout: Maximum seconds to wait.
        check_exit_code: If true, marks ok=False on non-zero exit code.
    """
    station = manager.get_station(station_id)
    ssh = station.get("ssh")
    if not ssh or not ssh.is_connected:
        return {"error": "SSH transport is offline or not configured"}
    result = ssh.safe_run(command, timeout_s=timeout, check_exit_code=check_exit_code)
    return {
        "command": result.command,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exited,
        "ok": result.ok,
        "duration_s": result.duration_s,
    }


@mcp.tool()
def board_run_serial(
    station_id: str,
    command: str,
    timeout: float = 30.0,
    check_exit_code: bool = True,
) -> dict:
    """Execute a shell command strictly over the UART serial transport.

    Args:
        station_id: Station identifier.
        command: Shell command to execute.
        timeout: Maximum seconds to wait.
        check_exit_code: If true, marks ok=False on non-zero exit code.
    """
    station = manager.get_station(station_id)
    serial = station.get("serial")
    if not serial or not serial.is_connected:
        return {"error": "Serial transport is offline or not configured"}
    result = serial.safe_run(command, timeout_s=timeout, check_exit_code=check_exit_code)
    return {
        "command": result.command,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exited,
        "ok": result.ok,
        "duration_s": result.duration_s,
    }


@mcp.tool()
def board_expect(station_id: str, pattern: str, timeout: float = 30.0) -> dict:
    """Wait for a regex pattern on the serial stream.

    Args:
        station_id: Station identifier.
        pattern: Regex pattern to match.
        timeout: Maximum seconds to wait.
    """
    station = manager.get_station(station_id)
    serial = station.get("serial")
    if not serial:
        return {"matched": False, "error": "No serial transport configured"}
    matched = serial.expect(pattern, timeout_s=timeout)
    return {"matched": matched, "pattern": pattern}


@mcp.tool()
def board_raw_write(station_id: str, data: str) -> dict:
    """Send raw ASCII/UTF-8 data directly to the board's serial port.

    Args:
        station_id: Station identifier.
        data: String payload to write.
    """
    station = manager.get_station(station_id)
    serial = station.get("serial")
    if not serial or not serial.is_connected:
        return {"error": "Serial port is not open"}
    serial.raw_write(data.encode("utf-8"))
    return {"success": True, "bytes_written": len(data.encode("utf-8"))}


# ── System Info & Manifest ───────────────────────────────────────

@mcp.tool()
def manifest_harvest(station_id: str) -> dict:
    """Harvest full hardware manifest from the board (S/N, MACs, SoC, RAM, eMMC).

    Args:
        station_id: Station identifier.
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.manifest import ManifestScraper

    scraper = ManifestScraper(station["transport"])
    manifest = scraper.harvest()
    return manifest.model_dump()


@mcp.tool()
def board_read_file(station_id: str, path: str) -> dict:
    """Read file content from the target board.

    Args:
        station_id: Station identifier.
        path: Absolute path on the target.
    """
    station = manager.get_station(station_id)
    result = station["transport"].safe_run(f"cat {path}", check_exit_code=False)
    return {"path": path, "content": result.stdout, "ok": result.ok}


@mcp.tool()
def board_write_file(station_id: str, path: str, content: str) -> dict:
    """Write string content to a file on the target board via heredoc.

    Args:
        station_id: Station identifier.
        path: Absolute path on the target.
        content: Text content to write.
    """
    station = manager.get_station(station_id)
    cmd = f"cat << 'MES_EOF' > {path}\n{content}\nMES_EOF"
    result = station["transport"].safe_run(cmd, check_exit_code=True)
    return {"path": path, "success": result.ok, "exit_code": result.exited}


@mcp.tool()
def board_file_exists(station_id: str, path: str) -> dict:
    """Check whether a file exists on the target board.

    Args:
        station_id: Station identifier.
        path: Path to test.
    """
    station = manager.get_station(station_id)
    result = station["transport"].safe_run(f"test -e {path}", check_exit_code=False)
    return {"path": path, "exists": result.ok}


# ── Protocol Validators ──────────────────────────────────────────

@mcp.tool()
def validate_ethernet_link(station_id: str, interface_key: str = "primary") -> dict:
    """Verify physical Ethernet link state and negotiated speed.

    Args:
        station_id: Station identifier.
        interface_key: Configuration key from station TOML [ethernet.<key>].
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.ethernet import EthernetValidator

    config = station["config"]
    if interface_key not in config.ethernet:
        return {"error": f"Interface key '{interface_key}' not in station ethernet config"}
    eth_cfg = config.ethernet[interface_key]
    result = EthernetValidator.setup_and_verify_link(station["transport"], eth_cfg)
    return result.model_dump()


@mcp.tool()
def validate_ethernet_throughput(station_id: str, interface_key: str = "primary") -> dict:
    """Measure Ethernet MAC/DMA throughput using iperf3.

    Args:
        station_id: Station identifier.
        interface_key: Configuration key from station TOML [ethernet.<key>].
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.ethernet import EthernetValidator

    config = station["config"]
    if interface_key not in config.ethernet:
        return {"error": f"Interface key '{interface_key}' not in station ethernet config"}
    eth_cfg = config.ethernet[interface_key]
    result = EthernetValidator.measure_throughput(station["transport"], eth_cfg)
    return result.model_dump()


@mcp.tool()
def validate_can_bus(station_id: str, can_iface: str = "can0", bitrate: int = 500000) -> dict:
    """Configure and test CAN bus interface.

    Args:
        station_id: Station identifier.
        can_iface: CAN interface name on DUT.
        bitrate: CAN bitrate in bps.
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.can_bus import CanTopologyValidator

    validator = CanTopologyValidator(station["transport"])
    try:
        validator.configure_dut_interface(can_iface, bitrate=bitrate)
        return {"passed": True, "interface": can_iface, "bitrate": bitrate}
    except Exception as e:
        return {"passed": False, "error_msg": str(e)}


@mcp.tool()
def validate_i2c_bus(station_id: str, bus: int = 0) -> dict:
    """Scan I2C bus and return discovered device addresses.

    Args:
        station_id: Station identifier.
        bus: I2C bus number.
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.i2c_bus import I2cBus

    i2c = I2cBus(station["transport"])
    devices = i2c.detect(bus)
    return {"bus": bus, "devices": [f"0x{addr:02x}" for addr in devices]}


@mcp.tool()
def validate_emmc_health(station_id: str, device_path: str = "/dev/mmcblk0") -> dict:
    """Interrogate eMMC EXTCSD registers for health and Pre-EOL status.

    Args:
        station_id: Station identifier.
        device_path: Block device path on target.
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.block_storage import BlockDeviceValidator

    result = BlockDeviceValidator.verify_emmc_health(station["transport"], device_path=device_path)
    return result.model_dump()


@mcp.tool()
def validate_block_throughput(
    station_id: str,
    mount_point: str = "/mnt/sdcard",
    test_file_size_mb: int = 50,
    min_write_mbps: float = 10.0,
) -> dict:
    """Measure true physical write throughput of a block storage device.

    Args:
        station_id: Station identifier.
        mount_point: Target mount point.
        test_file_size_mb: Size in MB to write.
        min_write_mbps: Minimum acceptable write throughput.
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.block_storage import BlockDeviceValidator

    result = BlockDeviceValidator.measure_throughput(
        station["transport"],
        mount_point=mount_point,
        test_file_size_mb=test_file_size_mb,
        min_write_mbps=min_write_mbps,
    )
    return result.model_dump()


@mcp.tool()
def validate_ram(station_id: str, size_mb: int = 50, loops: int = 1) -> dict:
    """Stress RAM using memtester and check hardware EDAC counters.

    Args:
        station_id: Station identifier.
        size_mb: Memory size to test in MB.
        loops: Number of test iterations.
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.memory import RamValidator

    result = RamValidator.verify_ram_health(station["transport"], size_mb=size_mb, loops=loops)
    return result.model_dump()


@mcp.tool()
def validate_gpio_loopback(station_id: str, loopback_key: str = "primary") -> dict:
    """Verify physical signal propagation across a GPIO loopback pair.

    Args:
        station_id: Station identifier.
        loopback_key: Configuration key from [gpio_loopback.<key>].
    """
    station = manager.get_station(station_id)
    config = station["config"]
    if loopback_key not in config.gpio_loopback:
        return {"error": f"GPIO loopback key '{loopback_key}' not in station config"}
    from pytest_mes_core.protocols.gpio import GpioLoopbackValidator

    result = GpioLoopbackValidator.verify_loopback(
        station["transport"], config.gpio_loopback[loopback_key]
    )
    return result.model_dump()


# ── Hardware Buses & Sysfs ───────────────────────────────────────

@mcp.tool()
def i2c_read_byte(station_id: str, bus: int, chip_addr: str, reg_addr: str) -> dict:
    """Read a single byte from an I2C device register.

    Args:
        station_id: Station identifier.
        bus: Bus number.
        chip_addr: Device address (e.g. '0x50').
        reg_addr: Register address (e.g. '0x00').
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.i2c_bus import I2cBus

    i2c = I2cBus(station["transport"])
    val = i2c.get_byte(bus, chip_addr, reg_addr)
    return {"bus": bus, "chip": chip_addr, "reg": reg_addr, "value": f"0x{val:02x}", "int_val": val}


@mcp.tool()
def i2c_write_byte(station_id: str, bus: int, chip_addr: str, reg_addr: str, value: str) -> dict:
    """Write a single byte to an I2C device register.

    Args:
        station_id: Station identifier.
        bus: Bus number.
        chip_addr: Device address.
        reg_addr: Register address.
        value: Byte value to write (e.g. '0xFF').
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.i2c_bus import I2cBus

    i2c = I2cBus(station["transport"])
    i2c.set_byte(bus, chip_addr, reg_addr, value)
    return {"success": True}


@mcp.tool()
def sysfs_read(station_id: str, path: str) -> dict:
    """Read a sysfs attribute on the board.

    Args:
        station_id: Station identifier.
        path: Path in /sys/.
    """
    station = manager.get_station(station_id)
    result = station["transport"].safe_run(f"cat {path}", check_exit_code=False)
    return {"path": path, "value": result.stdout.strip(), "ok": result.ok}


@mcp.tool()
def sysfs_write(station_id: str, path: str, value: str) -> dict:
    """Write a sysfs attribute on the board.

    Args:
        station_id: Station identifier.
        path: Path in /sys/.
        value: Value to write.
    """
    station = manager.get_station(station_id)
    result = station["transport"].safe_run(f"echo '{value}' > {path}", check_exit_code=True)
    return {"path": path, "value": value, "success": result.ok}


# ── Power Supply Control ─────────────────────────────────────────

@mcp.tool()
def psu_enable_output(station_id: str) -> dict:
    """Enable power supply output."""
    station = manager.get_station(station_id)
    psu = station.get("psu")
    if not psu:
        return {"success": False, "error": "No PSU configured"}
    psu.enable_output()
    return {"success": True}


@mcp.tool()
def psu_disable_output(station_id: str) -> dict:
    """Disable power supply output."""
    station = manager.get_station(station_id)
    psu = station.get("psu")
    if not psu:
        return {"success": False, "error": "No PSU configured"}
    psu.disable_output()
    return {"success": True}


@mcp.tool()
def psu_set_voltage(station_id: str, voltage: float) -> dict:
    """Set power supply target voltage."""
    station = manager.get_station(station_id)
    psu = station.get("psu")
    if not psu:
        return {"success": False, "error": "No PSU configured"}
    psu.set_voltage(voltage)
    return {"success": True, "voltage_v": voltage}


@mcp.tool()
def psu_set_current_limit(station_id: str, current: float) -> dict:
    """Set power supply current limit."""
    station = manager.get_station(station_id)
    psu = station.get("psu")
    if not psu:
        return {"success": False, "error": "No PSU configured"}
    if hasattr(psu, "set_current_limit"):
        psu.set_current_limit(current)
    elif hasattr(psu, "set_current"):
        psu.set_current(current)
    return {"success": True, "current_a": current}


@mcp.tool()
def psu_measure(station_id: str) -> dict:
    """Measure actual voltage and current (and power/temperature if supported) from power supply."""
    station = manager.get_station(station_id)
    psu = station.get("psu")
    if not psu:
        return {"success": False, "error": "No PSU configured"}
    res = {
        "success": True,
        "voltage_v": psu.measure_voltage(),
        "current_a": psu.measure_current(),
    }
    if hasattr(psu, "read_power"):
        try:
            res["power_w"] = psu.read_power()
        except Exception:
            pass
    if hasattr(psu, "read_temperature"):
        try:
            res["temperature_c"] = psu.read_temperature()
        except Exception:
            pass
    if hasattr(psu, "read_input_voltage"):
        try:
            res["input_voltage_v"] = psu.read_input_voltage()
        except Exception:
            pass
    return res


# ── Provisioning & Flashing ──────────────────────────────────────

@mcp.tool()
def sd_mux_set_mode(station_id: str, mode: str, mux_key: str = "factory_media") -> dict:
    """Switch SD-Mux multiplexer routing to 'host', 'dut', or 'off'.

    Args:
        station_id: Station identifier.
        mode: Target state ('host', 'dut', or 'off').
        mux_key: SD-Mux identifier in station config (default: 'factory_media').
    """
    station = manager.get_station(station_id)
    sd_mux = station.get("sd_mux")
    if not sd_mux:
        config = station["config"]
        if mux_key not in config.usb_sd_mux:
            if config.usb_sd_mux:
                mux_cfg = next(iter(config.usb_sd_mux.values()))
            else:
                return {"error": f"No usb_sd_mux found with key '{mux_key}' in station config"}
        else:
            mux_cfg = config.usb_sd_mux[mux_key]
        from pytest_mes_core.host_adapters.sd_mux import HostUsbSdMuxAdapter

        sd_mux = HostUsbSdMuxAdapter(mux_cfg)
        station["sd_mux"] = sd_mux

    try:
        sd_mux._set_mux_state(mode)
        return {"status": "ok", "mode": mode, "device": sd_mux.device_path}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def flash_tezi(station_id: str, image_path: str = "", tezi_key: str = "os_ram_loader") -> dict:
    """Flash board via NXP UUU / TI DFU + Toradex Easy Installer.

    WARNING: Destructive operation.

    Args:
        station_id: Station identifier.
        image_path: Path to TEZI payload directory. Defaults to tezi_folder_path in station config.
        tezi_key: Key in tezi_provisioning config (defaults to 'os_ram_loader' or first available).
    """
    station = manager.get_station(station_id)
    config = station["config"]
    if tezi_key not in config.tezi_provisioning:
        if config.tezi_provisioning:
            tezi_cfg = next(iter(config.tezi_provisioning.values()))
        else:
            return {"error": f"Tezi key '{tezi_key}' not in station config"}
    else:
        tezi_cfg = config.tezi_provisioning[tezi_key]

    from pytest_mes_core.provisioning.tezi_uuu import UuuTeziProvisioner

    target_path = Path(image_path) if image_path else Path(tezi_cfg.tezi_folder_path)

    recovery_timeout = getattr(tezi_cfg, "usb_recovery_timeout_s", getattr(tezi_cfg, "recovery_timeout_s", 60))
    flash_timeout = getattr(tezi_cfg, "flash_timeout_s", 720)

    provisioner = UuuTeziProvisioner(
        wait_for_recovery_s=recovery_timeout,
        flash_timeout_s=flash_timeout,
    )
    success = provisioner.provision(
        image_path=target_path,
        serial_client=station.get("serial"),
        fsm=station.get("fsm"),
    )
    return {"success": success}


@mcp.tool()
def flash_mcu(station_id: str, firmware_path: str, base_address: int = 0x08000000) -> dict:
    """Flash MCU firmware via probe-rs, pyOCD, or OpenOCD.

    WARNING: Destructive operation.
    """
    station = manager.get_station(station_id)
    from pytest_mes_core.provisioning.mcu_flasher import McuProvisioner

    # swd transport can be attached to station
    swd = station.get("swd") or station.get("mcu_probe")
    if not swd:
        return {"error": "No MCU debug probe (SWD) transport configured for station"}
    provisioner = McuProvisioner(swd)
    success = provisioner.flash_firmware(Path(firmware_path), base_address=base_address)
    return {"success": success}


# ── U-Boot ───────────────────────────────────────────────────────

@mcp.tool()
def uboot_env_get(station_id: str, variable: str) -> dict:
    """Read a U-Boot environment variable."""
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.boot_env import UBootShell

    serial = station.get("serial")
    if not serial:
        return {"error": "No serial transport available for U-Boot"}
    uboot = UBootShell(serial)
    val = uboot.get_var(variable)
    return {"variable": variable, "value": val}


@mcp.tool()
def uboot_env_set(station_id: str, variable: str, value: str) -> dict:
    """Set a U-Boot environment variable."""
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.boot_env import UBootShell

    serial = station.get("serial")
    if not serial:
        return {"error": "No serial transport available for U-Boot"}
    uboot = UBootShell(serial)
    uboot.set_var(variable, value)
    return {"success": True, "variable": variable, "value": value}


@mcp.tool()
def uboot_save_env(station_id: str) -> dict:
    """Commit U-Boot RAM environment to persistent flash."""
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.boot_env import UBootShell

    serial = station.get("serial")
    if not serial:
        return {"error": "No serial transport available for U-Boot"}
    uboot = UBootShell(serial)
    uboot.save_env()
    return {"success": True}


@mcp.tool()
def uboot_ping(station_id: str, ip: str) -> dict:
    """Execute ping from U-Boot network stack."""
    station = manager.get_station(station_id)
    from pytest_mes_core.protocols.boot_env import UBootShell

    serial = station.get("serial")
    if not serial:
        return {"error": "No serial transport available for U-Boot"}
    uboot = UBootShell(serial)
    alive = uboot.ping(ip)
    return {"ip": ip, "is_alive": alive}


# ── Kernel Debug & Watchdog ──────────────────────────────────────

@mcp.tool()
def kernel_dmesg(station_id: str, filter_pattern: str = "", lines: int = 50) -> dict:
    """Get kernel log (dmesg) with optional grep filter."""
    station = manager.get_station(station_id)
    cmd = "dmesg"
    if filter_pattern:
        cmd += f" | grep -i '{filter_pattern}'"
    cmd += f" | tail -n {lines}"
    result = station["transport"].safe_run(cmd, check_exit_code=False)
    return {"output": result.stdout, "filter": filter_pattern}


@mcp.tool()
def kernel_config_check(station_id: str, config_name: str) -> dict:
    """Check running kernel CONFIG_* setting in /proc/config.gz."""
    station = manager.get_station(station_id)
    cmd = f"zcat /proc/config.gz | grep -w '{config_name}'"
    result = station["transport"].safe_run(cmd, check_exit_code=False)
    return {"config": config_name, "value": result.stdout.strip(), "present": result.ok}


@mcp.tool()
def device_tree_read(station_id: str, path: str) -> dict:
    """Read a device tree property from /proc/device-tree."""
    station = manager.get_station(station_id)
    for base in ["/proc/device-tree", "/sys/firmware/devicetree/base"]:
        result = station["transport"].safe_run(
            f"cat {base}/{path.lstrip('/')}", check_exit_code=False
        )
        if result.ok:
            return {"path": path, "value": result.stdout.strip().rstrip("\x00")}
    return {"path": path, "value": None, "error": "Property not found"}


@mcp.tool()
def watchdog_status(station_id: str) -> dict:
    """Check UART kernel watchdog panic detection state."""
    station = manager.get_station(station_id)
    wd = station.get("watchdog")
    if not wd:
        return {"active": False, "error": "No watchdog configured"}
    return {
        "active": True,
        "is_panicked": wd.is_panicked(),
        "is_muted": getattr(wd, "_muted", False),
    }


# ── Resources ────────────────────────────────────────────────────

@mcp.resource("stations://list")
def resource_stations_list() -> str:
    """List all active stations with their FSM state."""
    return json.dumps(manager.list_stations(), indent=2)


@mcp.resource("station://{station_id}/state")
def resource_station_state(station_id: str) -> str:
    """Current FSM state for a station."""
    station = manager.get_station(station_id)
    fsm = station.get("fsm")
    state_name = "NO_FSM"
    if fsm is not None:
        state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)
    return json.dumps({"state": state_name}, indent=2)


@mcp.resource("station://{station_id}/config")
def resource_station_config(station_id: str) -> str:
    """Station configuration dictionary."""
    station = manager.get_station(station_id)
    return json.dumps(station["config"].model_dump(), indent=2)


# ── Prompts ──────────────────────────────────────────────────────

@mcp.prompt()
def board_health_check(station_id: str) -> str:
    """Comprehensive non-destructive board health check."""
    return f"""Perform a comprehensive health check on station '{station_id}':

1. Check physical state: fsm_state(station_id="{station_id}")
2. Boot to Linux if needed: fsm_boot_to_os(station_id="{station_id}")
3. Harvest hardware manifest: manifest_harvest(station_id="{station_id}")
4. Check kernel version: board_run(station_id="{station_id}", command="uname -r")
5. Check network link: validate_ethernet_link(station_id="{station_id}")
6. Interrogate eMMC health: validate_emmc_health(station_id="{station_id}")
7. Scan I2C buses: validate_i2c_bus(station_id="{station_id}", bus=0)
8. Check watchdog state: watchdog_status(station_id="{station_id}")
9. Check kernel dmesg for warnings: kernel_dmesg(station_id="{station_id}", filter_pattern="error")

Report a summary table with PASS/FAIL for each subsystem."""


@mcp.prompt()
def debug_boot_failure(station_id: str) -> str:
    """Diagnose a board that fails to boot."""
    return f"""Debug boot failure on station '{station_id}':

1. Measure PSU current/voltage: psu_measure(station_id="{station_id}")
2. Check kernel watchdog for panics: watchdog_status(station_id="{station_id}")
3. Drop and restore power: fsm_power_off(station_id="{station_id}") then fsm_energize(station_id="{station_id}")
4. Wait for serial output: board_expect(station_id="{station_id}", pattern="login:", timeout=60.0)
5. Read kernel ring buffer: kernel_dmesg(station_id="{station_id}", filter_pattern="panic|oops|error")
6. Interrogate device tree model: device_tree_read(station_id="{station_id}", path="model")

Synthesize the failure mechanism and suggest recovery actions."""


# ── CLI Entry Point ──────────────────────────────────────────────

def main():
    """Main CLI entrypoint for pytest-mes-mcp."""
    mcp.run()


if __name__ == "__main__":
    main()
