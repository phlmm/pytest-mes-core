# src/pytest_mes_core/host_adapters/serial_uart.py
import os
import logging
from typing import Optional, Any

# pyserial is cross-platform, but we still shield against ImportError
# in case the environment is missing dependencies.
try:
    import serial  # type: ignore
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False
    serial = None

from pytest_mes_core.config import HostSerialConfig
from pytest_mes_core.host_adapters.base import (
    BaseHostAdapter,
    HostAdapterError,
    HostResourceBusyError,
    HostHardwareDisconnectError
)

logger = logging.getLogger("mes_core.host_adapters.serial_uart")

class HostSerialError(HostAdapterError):
    """Specific exception for general Host UART/Serial failures."""
    pass

class HostSerialAdapter(BaseHostAdapter):
    """
    Zero-leakage manager for the x86 Host PC's USB-to-RS485/RS232 FTDI adapter.
    Features exclusive port locking to prevent split-brain byte stealing,
    pre-flight OS checks, and robust domain exception mapping.
    """

    def __init__(self, cfg: HostSerialConfig):
        self.cfg = cfg
        self.ser: Optional['serial.Serial'] = None

    @property
    def baudrate(self) -> int:
        """Exposes the configured baudrate for protocols (e.g., UartEchoValidator) to dynamically read."""
        return self.cfg.baudrate

    def __enter__(self) -> 'HostSerialAdapter':
        if not HAS_SERIAL:
            raise HostAdapterError("pyserial library is not installed in this environment.")

        logger.debug(f"[Host Serial] Opening {self.cfg.port} at {self.cfg.baudrate} baud...")

        # ==========================================
        # 1. OS-LEVEL PRE-FLIGHT CHECK
        # ==========================================
        # We only run this on Unix systems. Windows COM ports are handled by the try/except block.
        if self.cfg.port.startswith("/dev/") and not os.path.exists(self.cfg.port):
            raise HostHardwareDisconnectError(
                f"Serial port '{self.cfg.port}' does not exist. Is the FTDI cable unplugged?"
            )

        # ==========================================
        # 2. SOCKET BINDING & LOCKING
        # ==========================================
        try:
            # exclusive=True is the magic bullet here. It instructs the OS kernel to reject
            # any other process trying to open this port while Pytest owns it.
            self.ser = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=self.cfg.timeout_s,
                exclusive=True
            )

            # ==========================================
            # 3. KERNEL BUFFER SANITIZATION
            # ==========================================
            # DEFENSIVE: Purge floating hardware noise generated during USB enumeration
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

        except serial.SerialException as e:
            err_str = str(e).lower()
            logger.error(f"[Host Serial] Failed to claim {self.cfg.port}: {e}")

            # Intelligently map raw OS errors into our global framework exceptions
            if "device or resource busy" in err_str or "access is denied" in err_str:
                raise HostResourceBusyError(f"Serial port {self.cfg.port} is locked by another terminal (minicom/screen?).")
            elif "file not found" in err_str or "no such file" in err_str:
                raise HostHardwareDisconnectError(f"Serial port {self.cfg.port} physically disconnected: {e}")
            else:
                raise HostSerialError(f"Host Serial hardware failure on {self.cfg.port}: {e}")

        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Safely releases the COM port and OS locks."""
        if self.ser and self.ser.is_open:
            try:
                logger.debug(f"[Host Serial] ZERO-LEAKAGE: Releasing {self.cfg.port}.")
                self.ser.close()
            except Exception as e:
                logger.warning(f"[Host Serial] Teardown exception during port closure: {e}")
            finally:
                self.ser = None
