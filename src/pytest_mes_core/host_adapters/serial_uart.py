import serial  # type: ignore
import logging
from typing import Optional

from pytest_mes_core.config import HostSerialConfig

logger = logging.getLogger("mes_core.host_adapters.serial_uart")

class HostSerialAdapter:
    """Zero-leakage manager for the x86 Host PC's USB-to-RS485/RS232 FTDI adapter."""

    def __init__(self, cfg: HostSerialConfig):
        self.cfg = cfg
        self.ser: Optional[serial.Serial] = None

    def __enter__(self) -> 'HostSerialAdapter':
        logger.debug(f"[Host Serial] Opening {self.cfg.port} at {self.cfg.baudrate} baud...")
        try:
            self.ser = serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=self.cfg.timeout_s)
            # DEFENSIVE: Purge floating hardware noise generated during USB enumeration
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except serial.SerialException as e:
            logger.error(f"[Host Serial] Failed to claim {self.cfg.port}: {e}")
            raise RuntimeError(f"FATAL: Host Serial failure on {self.cfg.port}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.ser and self.ser.is_open:
            logger.debug(f"[Host Serial] ZERO-LEAKAGE: Releasing {self.cfg.port}.")
            self.ser.close()
