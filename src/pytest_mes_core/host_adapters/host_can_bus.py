import time
import logging
from typing import Optional, Any
try:
    import can # type: ignore
except ImportError:
    can = None

from pytest_mes_core.config import HostCanConfig
from pytest_mes_core.host_adapters.base import BaseHostAdapter, HostAdapterError

logger = logging.getLogger("mes_core.host_adapters.can")

class HostCanAdapter(BaseHostAdapter):
    """
    Natively bridges 'socketcan' and 'slcan' USB dongles.
    Complies with the BaseHostAdapter Zero-Leakage contract via __enter__/__exit__.
    """
    def __init__(self, cfg: HostCanConfig):
        self.cfg = cfg
        self.bus: Optional['can.BusABC'] = None
        self.listener: Optional['can.BufferedReader'] = None
        self.notifier: Optional['can.Notifier'] = None

    def __enter__(self) -> 'HostCanAdapter':
        self.connect()
        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Stop the background Notifier thread and release the CAN socket."""
        self.disconnect()

    def connect(self) -> None:
        if can is None: raise HostAdapterError("python-can package is not installed.")
        if self.bus: return
        logger.info(f"[Host CAN] Binding {self.cfg.bustype} on {self.cfg.interface} @ {self.cfg.bitrate}bps...")

        kwargs = {"interface": self.cfg.bustype, "channel": self.cfg.interface, "bitrate": self.cfg.bitrate}
        if self.cfg.bustype == "slcan":
            kwargs["tty_baudrate"] = self.cfg.tty_baudrate # Required for FTDI chips inside CAN dongles
        elif self.cfg.bustype == "socketcan":
            import subprocess
            import os
            # Ensure the host interface is up with the correct bitrate (ignoring errors if we lack sudo)
            subprocess.run(["sudo", "-n", "ip", "link", "set", self.cfg.interface, "down"], capture_output=True)
            subprocess.run(["sudo", "-n", "ip", "link", "set", self.cfg.interface, "type", "can", "bitrate", str(self.cfg.bitrate)], capture_output=True)
            subprocess.run(["sudo", "-n", "ip", "link", "set", self.cfg.interface, "up"], capture_output=True)
            
            # ENVIRONMENT CHECK: Verify the interface actually came UP
            sysfs_path = f"/sys/class/net/{self.cfg.interface}/operstate"
            if os.path.exists(sysfs_path):
                with open(sysfs_path, "r") as f:
                    state = f.read().strip()
                if state == "down":
                    raise HostAdapterError(
                        f"FATAL: CAN Interface '{self.cfg.interface}' is DOWN. "
                        f"The automated 'sudo ip link set up' command failed (likely due to sudo password prompt). "
                        f"Please bring the interface up manually: sudo ip link set {self.cfg.interface} up"
                    )

        try:
            self.bus = can.Bus(**kwargs)
            self.listener = can.BufferedReader()
            self.notifier = can.Notifier(self.bus, [self.listener])
        except Exception as e:
            # Clean up partial state if binding fails mid-way
            self.disconnect()
            raise HostAdapterError(f"Failed to bind CAN bus {self.cfg.interface}: {e}")

    def disconnect(self) -> None:
        if self.notifier:
            try:
                self.notifier.stop()
            except Exception as e:
                logger.debug(f"[Host CAN] Notifier stop failed: {e}")
            self.notifier = None
        if self.bus:
            try:
                self.bus.shutdown()
            except Exception as e:
                logger.debug(f"[Host CAN] Bus shutdown failed: {e}")
            self.bus = None
        self.listener = None

    def clear_rx_buffer(self) -> None:
        if self.listener:
            while self.listener.get_message(timeout=0.0): pass

    def send(self, can_id: int, payload: bytes) -> None:
        if not self.bus: raise HostAdapterError("Host CAN not connected.")
        msg = can.Message(arbitration_id=can_id, data=payload, is_extended_id=False)
        self.bus.send(msg)

    def expect(self, expected_id: int, expected_payload: bytes, timeout_s: float = 2.0) -> bool:
        if not self.listener: raise HostAdapterError("Host CAN not connected.")
        t_end = time.perf_counter() + timeout_s
        while time.perf_counter() < t_end:
            msg = self.listener.get_message(timeout=0.1)
            if msg and msg.arbitration_id == expected_id and bytes(msg.data) == expected_payload:
                return True
        return False
