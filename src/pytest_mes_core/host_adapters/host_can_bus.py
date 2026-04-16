import time
import logging
try:
    import can # type: ignore
except ImportError:
    can = None
from typing import Optional
from pytest_mes_core.config import HostCanConfig

logger = logging.getLogger("mes_core.host_adapters.can")

class HostCanAdapter:
    """Natively bridges 'socketcan' and 'slcan' USB dongles."""
    def __init__(self, cfg: HostCanConfig):
        self.cfg = cfg
        self.bus: Optional[can.BusABC] = None
        self.listener: Optional[can.BufferedReader] = None
        self.notifier: Optional[can.Notifier] = None

    def connect(self) -> None:
        if can is None: raise RuntimeError("python-can package is not installed.")
        if self.bus: return
        logger.info(f"[Host CAN] Binding {self.cfg.bustype} on {self.cfg.interface} @ {self.cfg.bitrate}bps...")

        kwargs = {"interface": self.cfg.bustype, "channel": self.cfg.interface, "bitrate": self.cfg.bitrate}
        if self.cfg.bustype == "slcan":
            kwargs["tty_baudrate"] = self.cfg.tty_baudrate # Required for FTDI chips inside CAN dongles

        self.bus = can.Bus(**kwargs)
        self.listener = can.BufferedReader()
        self.notifier = can.Notifier(self.bus, [self.listener])

    def disconnect(self) -> None:
        if self.notifier: self.notifier.stop()
        if self.bus:
            self.bus.shutdown()
            self.bus = None

    def clear_rx_buffer(self) -> None:
        if self.listener:
            while self.listener.get_message(timeout=0.0): pass

    def send(self, can_id: int, payload: bytes) -> None:
        if not self.bus: raise RuntimeError("Host CAN not connected.")
        msg = can.Message(arbitration_id=can_id, data=payload, is_extended_id=False)
        self.bus.send(msg)

    def expect(self, expected_id: int, expected_payload: bytes, timeout_s: float = 2.0) -> bool:
        if not self.listener: raise RuntimeError("Host CAN not connected.")
        t_end = time.perf_counter() + timeout_s
        while time.perf_counter() < t_end:
            msg = self.listener.get_message(timeout=0.1)
            if msg and msg.arbitration_id == expected_id and bytes(msg.data) == expected_payload:
                return True
        return False
