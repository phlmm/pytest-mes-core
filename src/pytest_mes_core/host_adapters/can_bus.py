import can  # type: ignore
import logging
from typing import Optional

from pytest_mes_core.config import HostCanConfig

logger = logging.getLogger("mes_core.host_adapters.can_bus")

class HostCanAdapter:
    """Zero-leakage manager for the x86 Host PC's SocketCAN interface."""

    def __init__(self, cfg: HostCanConfig):
        self.cfg = cfg
        self.bus: Optional[can.interface.Bus] = None

    def __enter__(self) -> 'HostCanAdapter':
        logger.debug(f"[Host CAN] Binding to {self.cfg.interface} at {self.cfg.bitrate}bps...")
        try:
            self.bus = can.interface.Bus(
                channel=self.cfg.interface,
                bustype='socketcan',
                bitrate=self.cfg.bitrate
            )
        except can.CanError as e:
            logger.error(f"[Host CAN] Failed to bind to {self.cfg.interface}. Interface up? Error: {e}")
            raise RuntimeError(f"FATAL: Host CAN socket failure on {self.cfg.interface}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.bus:
            logger.debug(f"[Host CAN] ZERO-LEAKAGE: Shutting down socket on {self.cfg.interface}.")
            self.bus.shutdown()
