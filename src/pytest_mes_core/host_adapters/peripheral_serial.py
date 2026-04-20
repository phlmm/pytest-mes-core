import time
import logging
from typing import Optional, Any

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

logger = logging.getLogger("mes_core.host_adapters.peripheral_serial")

class HostPeripheralSerialAdapter(BaseHostAdapter):
    """
    Raw binary serial adapter for testing RS485/UART data buses.
    Complies with the BaseHostAdapter Zero-Leakage contract via __enter__/__exit__.
    Uses exclusive port locking to prevent split-brain byte stealing in pytest-xdist.
    """
    def __init__(self, cfg: HostSerialConfig):
        self.cfg = cfg
        self.ser: Optional['serial.Serial'] = None

    def __enter__(self) -> 'HostPeripheralSerialAdapter':
        self.connect()
        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Release the COM port and OS locks."""
        self.disconnect()

    def connect(self) -> None:
        """Acquires an exclusive OS lock on the target COM port.

        Clears the hardware UART FIFOs after connection to prevent stale bytes
        from interfering with tests.

        Raises:
            HostResourceBusyError: If another process (e.g., minicom) owns the port.
            HostHardwareDisconnectError: If the port does not physically exist.
            HostAdapterError: If pyserial is missing or another hardware failure occurs.
        """
        if not HAS_SERIAL:
            raise HostAdapterError("pyserial library is not installed on the Host PC environment.")
        if self.ser: return
        logger.info(f"[Host RS485] Binding on {self.cfg.port} @ {self.cfg.baudrate}bps")

        try:
            self.ser = serial.Serial(
                self.cfg.port,
                self.cfg.baudrate,
                timeout=self.cfg.timeout_s,
                exclusive=True  # Prevent split-brain byte stealing in multi-jig xdist
            )
            # Purge residual bytes from previous test runs
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except serial.SerialException as e:
            err_str = str(e).lower()
            if "device or resource busy" in err_str or "access is denied" in err_str:
                raise HostResourceBusyError(f"Serial port {self.cfg.port} is locked by another process: {e}")
            elif "file not found" in err_str or "no such file" in err_str:
                raise HostHardwareDisconnectError(f"Serial port physically disconnected: {self.cfg.port}")
            else:
                raise HostAdapterError(f"Hardware failure on {self.cfg.port}: {e}")

    def disconnect(self) -> None:
        """Safely closes the serial port and releases the OS lock."""
        if self.ser:
            try:
                self.ser.close()
            except Exception as e:
                logger.warning(f"[Host RS485] Teardown exception during port closure: {e}")
            finally:
                self.ser = None

    def clear_rx_buffer(self) -> None:
        """Flushes the input buffer of the serial port."""
        if self.ser: self.ser.reset_input_buffer()

    def send(self, payload: bytes) -> None:
        """Transmits a binary payload over the serial interface and blocks until flushed.

        Args:
            payload: The raw bytes to send.

        Raises:
            HostAdapterError: If the port is not connected.
        """
        if not self.ser:
            raise HostAdapterError("Host RS485 not connected.")
        self.ser.write(payload)
        self.ser.flush()

    def expect(self, payload: bytes, timeout_s: float = 2.0) -> bool:
        """Blocks until the exact byte sequence is received or the timeout expires.

        Args:
            payload: The exact byte sequence to search for in the incoming stream.
            timeout_s: Maximum time to wait in seconds.

        Returns:
            bool: True if the payload was found, False otherwise.
        """
        if not self.ser: return False
        t_end = time.perf_counter() + timeout_s
        buf = bytearray()
        while time.perf_counter() < t_end:
            if self.ser.in_waiting > 0:
                buf.extend(self.ser.read(self.ser.in_waiting))
                if payload in buf:
                    return True
            time.sleep(0.01)
        return False
