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
from pytest_mes_core.host_adapters import (
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
        """Connects to the serial port with exclusive OS locking.

        Returns:
            HostSerialAdapter: The initialized and connected serial adapter.

        Raises:
            HostAdapterError: If pyserial is missing.
            HostResourceBusyError: If the port is locked by another process.
            HostHardwareDisconnectError: If the port doesn't exist.
            HostSerialError: For other hardware-level failures.
        """
        if not HAS_SERIAL:
            err_msg = "pyserial library is not installed on the Host PC environment."
            logger.critical("="*60)
            logger.critical(f"[Host Serial] FATAL: {err_msg}")
            logger.critical("="*60)
            raise HostAdapterError(err_msg)

        logger.debug(f"[Host Serial] Attempting exclusive OS lock on {self.cfg.port} at {self.cfg.baudrate} baud...")

        try:
            self.ser = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=self.cfg.timeout_s,
                exclusive=True  # Asks the OS to lock the port
            )

            # Matrix Tracing: Explicitly log the FIFO buffer purge
            logger.debug(f"[Host Serial] OS Lock acquired. Flushing residual hardware FIFO buffers...")
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            logger.info(f"[Host Serial] Hardware adapter ready on {self.cfg.port} ({self.cfg.baudrate} baud).")

        except serial.SerialException as e:
            err_str = str(e).lower()

            if "device or resource busy" in err_str or "access is denied" in err_str:
                # Ask the Kernel who owns the port
                owner = ResourceDiagnostics.get_device_owner(self.cfg.port)

                logger.critical("="*60)
                if owner:
                    error_msg = f"Serial port {self.cfg.port} is locked by PID/Process: {owner}!"
                    logger.critical(f"[Host Serial] FATAL: {error_msg}")
                    logger.critical("[Host Serial] Please close the competing application (minicom, Putty) and retry.")
                else:
                    error_msg = f"Serial port {self.cfg.port} is busy (OS refused to identify owner)."
                    logger.critical(f"[Host Serial] FATAL: {error_msg}")
                logger.critical("="*60)

                raise HostResourceBusyError(error_msg)

            elif "file not found" in err_str or "no such file" in err_str:
                error_msg = f"Serial port physically disconnected or missing: {self.cfg.port}"
                logger.critical("="*60)
                logger.critical(f"[Host Serial] FATAL: {error_msg}")
                logger.critical(f"[Host Serial] Check the USB/FTDI connection to the Host PC.")
                logger.critical("="*60)
                raise HostHardwareDisconnectError(error_msg)
            else:
                error_msg = f"Hardware failure on {self.cfg.port}: {e}"
                logger.critical("="*60)
                logger.critical(f"[Host Serial] FATAL: {error_msg}")
                logger.critical("="*60)
                raise HostSerialError(error_msg)

        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Safely releases the COM port and OS locks."""
        if self.ser and self.ser.is_open:
            try:
                logger.debug(f"[Host Serial] ZERO-LEAKAGE: Releasing OS lock on {self.cfg.port}.")
                self.ser.close()
            except Exception as e:
                # Downgraded to warning to avoid masking primary test exceptions
                logger.warning(f"[Host Serial] Teardown exception during port closure: {e}")
            finally:
                self.ser = None
