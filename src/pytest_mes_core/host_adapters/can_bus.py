# src/pytest_mes_core/host_adapters/can_bus.py
import os
import can  # type: ignore
import logging
from typing import Optional, Any

from pytest_mes_core.config import HostCanConfig
from pytest_mes_core.host_adapters.base import BaseHostAdapter, HostAdapterError

logger = logging.getLogger("mes_core.host_adapters.can")

class HostCanError(HostAdapterError):
    """Specific exception for Host SocketCAN failures."""
    pass

class HostCanAdapter(BaseHostAdapter):
    """
    Zero-leakage manager for the x86 Host PC's SocketCAN interface.
    Features OS-level pre-flight checks, automatic stale buffer flushing,
    and strict compliance with the BaseHostAdapter teardown contract.
    """

    def __init__(self, cfg: HostCanConfig):
        self.cfg = cfg
        self.bus: Optional[can.BusABC] = None

    @property
    def bitrate(self) -> int:
        """Exposes the configured bitrate for protocols to dynamically read."""
        return self.cfg.bitrate

    def __enter__(self) -> 'HostCanAdapter':
        logger.debug(f"[Host CAN] Initializing {self.cfg.interface} at {self.cfg.bitrate}bps...")

        # ==========================================
        # 1. OS-LEVEL PRE-FLIGHT CHECK
        # ==========================================
        sysfs_path = f"/sys/class/net/{self.cfg.interface}"
        if not os.path.exists(sysfs_path):
            raise HostCanError(
                f"Host interface '{self.cfg.interface}' does not exist in the OS. "
                f"Is the USB2CAN adapter physically unplugged?"
            )

        try:
            with open(f"{sysfs_path}/operstate", "r") as f:
                operstate = f.read().strip()
                if operstate == "down":
                    logger.warning(f"[Host CAN] {self.cfg.interface} is DOWN. Attempting to bind anyway (python-can may auto-up it)...")
        except PermissionError:
            pass # Ignore if running without sufficient privileges to read operstate

        # ==========================================
        # 2. SOCKET BINDING
        # ==========================================
        try:
            self.bus = can.interface.Bus(
                channel=self.cfg.interface,
                bustype='socketcan',
                bitrate=self.cfg.bitrate
            )
        except can.CanError as e:
            logger.error(f"[Host CAN] Failed to bind to {self.cfg.interface}. Error: {e}")
            raise HostCanError(f"SocketCAN bind failure on {self.cfg.interface}: {e}")

        # ==========================================
        # 3. KERNEL BUFFER SANITIZATION
        # ==========================================
        # Purge any stale frames sitting in the Linux RX buffer from previous tests
        flushed_count = 0
        while True:
            # Non-blocking read to clear the buffer instantly
            msg = self.bus.recv(timeout=0.0)
            if msg is None:
                break
            flushed_count += 1

        if flushed_count > 0:
            logger.debug(f"[Host CAN] Sanitized {flushed_count} stale ghost frames from kernel buffer.")

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Safely releases the SocketCAN file descriptor."""
        if self.bus:
            try:
                logger.debug(f"[Host CAN] ZERO-LEAKAGE: Shutting down socket on {self.cfg.interface}.")
                self.bus.shutdown()
            except Exception as e:
                # We log but do NOT raise here, otherwise we mask the original test exception
                logger.warning(f"[Host CAN] Teardown exception during bus shutdown: {e}")
            finally:
                self.bus = None
