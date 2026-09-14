"""Station session manager for MCP server."""

from __future__ import annotations

import glob
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger("mes_core.mcp.station_manager")


class StationManager:
    """Manages test station sessions for the MCP server.

    Loads station_env.toml configurations and instantiates the full
    FSM, failover transports, instruments, and kernel watchdog.
    """

    def __init__(self):
        self._stations: Dict[str, Dict[str, Any]] = {}

    def connect(
        self,
        station_id: str,
        config_path: Path,
        mock_transport: Optional[Any] = None,
    ) -> dict:
        """Load configuration and initialize a test station session.

        Args:
            station_id: Unique identifier for the station session.
            config_path: Path to the station_env.toml file.
            mock_transport: Optional mock transport for simulation/testing.

        Returns:
            dict containing connection status and initialized hardware details.
        """
        if station_id in self._stations:
            return {"error": f"Station '{station_id}' is already connected"}

        config_path = Path(config_path)
        if not config_path.exists():
            return {"error": f"Config file not found: {config_path}"}

        try:
            from pytest_mes_core.config import StationEnvironment

            with open(config_path, "rb") as f:
                data = tomllib.load(f)
            config = StationEnvironment.model_validate(data)

            serial = None
            ssh = None
            transport = None

            if mock_transport is not None:
                transport = mock_transport
            else:
                # Initialize Transports
                from pytest_mes_core.transports.failover import FailoverTransport
                from pytest_mes_core.transports.serial_client import EphemeralSerialClient
                from pytest_mes_core.transports.ssh import EphemeralSSHClient

                if "debug_port" in config.host_serial and config.host_serial["debug_port"].enabled:
                    serial = EphemeralSerialClient(config.host_serial["debug_port"])
                    try:
                        serial.connect()
                    except Exception as e:
                        logger.warning("Serial debug port connection deferred or failed", error=str(e))
                elif config.host_serial:
                    first_serial = next(iter(config.host_serial.values()))
                    if first_serial.enabled:
                        serial = EphemeralSerialClient(first_serial)
                        try:
                            serial.connect()
                        except Exception as e:
                            logger.warning("Serial connection deferred or failed", error=str(e))

                if "primary" in config.ssh_targets and config.ssh_targets["primary"].enabled:
                    ssh = EphemeralSSHClient(config.ssh_targets["primary"])
                    try:
                        ssh.connect()
                    except Exception:
                        logger.warning("SSH target offline, will rely on serial fallback")

                if ssh and serial:
                    transport = FailoverTransport(primary=ssh, fallback=serial)
                elif ssh:
                    transport = ssh
                elif serial:
                    transport = serial
                else:
                    return {"error": "No enabled serial or SSH transport configured in station environment"}

            # Initialize PSU if configured
            psu = self._init_psu(config)

            # Initialize FSM
            fsm = None
            if config.state_machine and config.state_machine.enabled:
                from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine

                fsm = EmbeddedLinuxStateMachine(
                    psu=psu,
                    serial=serial,
                    ssh=ssh,
                    cfg=config.state_machine,
                )

            # Initialize SD-Mux & wire SdMuxRecoveryStrategy if recovery_gpio is configured
            sd_mux = None
            if config.usb_sd_mux:
                mux_cfg = config.usb_sd_mux.get("factory_media") or next(iter(config.usb_sd_mux.values()), None)
                if mux_cfg and mux_cfg.enabled:
                    try:
                        from pytest_mes_core.host_adapters.sd_mux import HostUsbSdMuxAdapter, SdMuxRecoveryStrategy

                        sd_mux = HostUsbSdMuxAdapter(mux_cfg)
                        if fsm is not None and mux_cfg.recovery_gpio is not None:
                            fsm.recovery_strategy = SdMuxRecoveryStrategy(sd_mux, latch_time_s=5.0)
                            logger.info("SdMuxRecoveryStrategy injected into FSM", gpio=mux_cfg.recovery_gpio)
                    except Exception as e:
                        logger.warning("Failed to initialize SD-Mux adapter", error=str(e))

            # Initialize Watchdog if serial is active
            watchdog = None
            if serial is not None:
                from pytest_mes_core.transports.watchdog import UartKernelWatchdog

                watchdog = UartKernelWatchdog(serial)
                try:
                    watchdog.start()
                except Exception as e:
                    logger.warning("Failed to start UART watchdog thread", error=str(e))

            self._stations[station_id] = {
                "config": config,
                "config_path": config_path,
                "fsm": fsm,
                "transport": transport,
                "serial": serial,
                "ssh": ssh,
                "psu": psu,
                "sd_mux": sd_mux,
                "watchdog": watchdog,
            }

            state_name = "UNKNOWN"
            if fsm is not None:
                state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)

            logger.info("Station connected", station_id=station_id, state=state_name)
            return {
                "station_id": station_id,
                "state": state_name,
                "transport_type": type(transport).__name__,
                "has_fsm": fsm is not None,
                "has_psu": psu is not None,
                "has_watchdog": watchdog is not None,
            }

        except Exception as e:
            logger.exception("Failed to connect station", station_id=station_id, error=str(e))
            return {"error": str(e)}

    def _init_psu(self, config) -> Any:
        """Initialize power supply from config.psu_hardware."""
        try:
            if not config.psu_hardware or not config.psu_hardware.enabled:
                return None

            if config.psu_hardware.vendor == "fnirsi":
                from pytest_mes_core.instruments.fnirsi_dps150 import DPS150

                psu = DPS150(
                    port=config.psu_hardware.serial_port,
                    baud=config.psu_hardware.baudrate,
                )
                psu.connect()
                if getattr(config.psu_hardware, "ovp_limit", None) is not None:
                    psu.set_ovp(config.psu_hardware.ovp_limit)
                if getattr(config.psu_hardware, "ocp_limit", None) is not None:
                    psu.set_ocp(config.psu_hardware.ocp_limit)
                if getattr(config.psu_hardware, "default_voltage", None) is not None:
                    psu.set_voltage(config.psu_hardware.default_voltage)
                    psu.default_voltage = config.psu_hardware.default_voltage
                if getattr(config.psu_hardware, "default_current", None) is not None:
                    psu.set_current(config.psu_hardware.default_current)
                    psu.default_current = config.psu_hardware.default_current
                return psu
            else:
                from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply

                psu = ScpiPowerSupply(config.psu_hardware)
                psu.connect()
                if getattr(config.psu_hardware, "default_voltage", None) is not None:
                    psu.default_voltage = config.psu_hardware.default_voltage
                if getattr(config.psu_hardware, "default_current", None) is not None:
                    psu.default_current = config.psu_hardware.default_current
                return psu
        except Exception as e:
            logger.warning("PSU initialization failed", error=str(e))
            return None

    def disconnect(self, station_id: str) -> dict:
        """Disconnect and tear down station resources safely."""
        station = self._stations.pop(station_id, None)
        if not station:
            return {"error": f"Station '{station_id}' not found"}

        try:
            if station.get("watchdog"):
                try:
                    station["watchdog"].stop()
                except Exception:
                    pass

            if station.get("psu"):
                try:
                    station["psu"].disable_output()
                    if hasattr(station["psu"], "close"):
                        station["psu"].close()
                except Exception:
                    pass

            if station.get("sd_mux"):
                try:
                    station["sd_mux"]._set_mux_state("host")
                except Exception:
                    pass

            for key in ["ssh", "serial"]:
                client = station.get(key)
                if client:
                    try:
                        client.disconnect()
                    except Exception:
                        pass

            return {"success": True, "station_id": station_id}
        except Exception as e:
            return {"error": str(e)}

    def get_station(self, station_id: str) -> Dict[str, Any]:
        """Get station dictionary or raise ValueError if not connected."""
        station = self._stations.get(station_id)
        if not station:
            raise ValueError(
                f"Station '{station_id}' is not connected. "
                "Call station_connect(station_id, config_path) first."
            )
        return station

    def list_stations(self) -> list[dict]:
        """List active station identifiers and current states."""
        res = []
        for sid, s in self._stations.items():
            fsm = s.get("fsm")
            state_name = "NO_FSM"
            if fsm is not None:
                state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)

            res.append(
                {
                    "station_id": sid,
                    "state": state_name,
                    "transport": type(s["transport"]).__name__,
                    "has_psu": s.get("psu") is not None,
                }
            )
        return res

    def get_status(self, station_id: str) -> dict:
        """Query comprehensive status for a station."""
        s = self.get_station(station_id)
        fsm = s.get("fsm")
        state_name = "NO_FSM"
        if fsm is not None:
            state_name = fsm.state.name if hasattr(fsm.state, "name") else str(fsm.state)

        status = {
            "station_id": station_id,
            "state": state_name,
            "transport_type": type(s["transport"]).__name__,
            "serial_connected": s["serial"].is_connected if s.get("serial") else False,
            "ssh_connected": s["ssh"].is_connected if s.get("ssh") else False,
            "has_psu": s.get("psu") is not None,
        }

        psu = s.get("psu")
        if psu and hasattr(psu, "measure_voltage"):
            try:
                status["psu_voltage_v"] = psu.measure_voltage()
                status["psu_current_a"] = psu.measure_current()
            except Exception:
                pass

        wd = s.get("watchdog")
        if wd:
            status["watchdog_panicked"] = wd.is_panicked()

        return status

    def discover(self) -> dict:
        """Scan system for serial ports and recovery mode USB devices."""
        serial_ports = sorted(
            glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/serial/by-id/*")
        )
        usb_devices = []
        try:
            result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if any(vid in line for vid in ["0451:6165", "1fc9:", "15a2:", "1b67:"]):
                    usb_devices.append({"line": line.strip(), "type": "recovery_mode"})
        except Exception:
            pass

        return {
            "serial_ports": serial_ports,
            "recovery_devices": usb_devices,
        }
