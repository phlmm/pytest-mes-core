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

from pytest_mes_core.host_adapters.diagnostics import ResourceDiagnostics

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
        logger.debug(f"[Host Serial] Opening {self.cfg.port} at {self.cfg.baudrate} baud...")

        try:
            self.ser = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=self.cfg.timeout_s,
                exclusive=True  # Asks the OS to lock the port
            )
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

        except serial.SerialException as e:
            err_str = str(e).lower()

            if "device or resource busy" in err_str or "access is denied" in err_str:
                # THE UPGRADE: Ask the Kernel who owns the port!
                owner = ResourceDiagnostics.get_device_owner(self.cfg.port)

                if owner:
                    error_msg = f"Serial port {self.cfg.port} is locked by {owner}!"
                    logger.critical(f"[Host Serial] {error_msg} Please close it and retry.")
                    raise HostResourceBusyError(error_msg)
                else:
                    raise HostResourceBusyError(f"Serial port {self.cfg.port} is busy (OS refused to identify owner).")

            elif "file not found" in err_str or "no such file" in err_str:
                raise HostHardwareDisconnectError(f"Serial port physically disconnected: {self.cfg.port}")
            else:
                raise HostSerialError(f"Hardware failure: {e}")

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
