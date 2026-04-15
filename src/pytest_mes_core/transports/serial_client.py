# src/pytest_mes_core/transports/serial_client.py
import time
import logging
import serial # type: ignore
from contextlib import contextmanager
from typing import Generator, Optional, Tuple

from pytest_mes_core.config import HostSerialConfig
from pytest_mes_core.transports.base import TransportConnectionError, TransportTimeoutError

logger = logging.getLogger("mes_core.transports.serial")

class EphemeralSerialClient:
    """
    Unified Expect Engine for UART.
    Handles EMI noise decoding, Prompt-Aware command execution,
    and Exclusive Locks for raw binary tests.
    """
    def __init__(self, cfg: HostSerialConfig):
        self.cfg = cfg
        self.ser: Optional[serial.Serial] = None
        self._is_locked = False

    def connect(self) -> None:
        try:
            self.ser = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=self.cfg.timeout_s,
                exclusive=True # OS-level lock so no other Host PC app steals the port
            )
        except serial.SerialException as e:
            raise TransportConnectionError(f"Failed to bind Host UART {self.cfg.port}: {e}")

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.close()

    @property
    def is_connected(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    # ==========================================
    # THE UNIFIED 'EXPECT' ENGINE
    # ==========================================
    def expect(self, pattern: str, timeout_s: float = 5.0, blast_char: str = "") -> str:
        """
        Reads from UART until the string pattern is found or timeout occurs.
        Optionally blasts a character (like \n) to defeat UART FIFO sleep states.
        """
        if not self.is_connected:
            raise TransportConnectionError("Serial port is closed.")
        if self._is_locked:
            raise RuntimeError("Cannot use expect() while UART is strictly locked by a test.")

        self.ser.timeout = timeout_s
        pattern_bytes = pattern.encode('utf-8')

        if blast_char:
            for _ in range(3):
                self.ser.write(blast_char.encode('utf-8'))
                time.sleep(0.05)

        logger.debug(f"[UART] Expecting '{pattern}' (Timeout: {timeout_s}s)...")

        # Read until the pattern is found
        raw_bytes = self.ser.read_until(pattern_bytes)
        decoded = raw_bytes.decode('utf-8', errors='replace') # EMI resistant

        if pattern not in decoded:
            logger.error(f"[UART] Timeout expecting '{pattern}'. Buffer yielded: {decoded.strip()[-100:]}")
            raise TransportTimeoutError(f"UART Expect Timeout: '{pattern}' not found.")

        return decoded

    def write_line(self, cmd: str) -> None:
        """Safely injects a string into the TX line."""
        if self._is_locked:
            raise RuntimeError("Cannot use write_line() while UART is locked.")

        logger.debug(f"[UART] TX -> '{cmd}'")
        self.ser.write(f"{cmd}\n".encode('utf-8'))
        self.ser.flush()

    def safe_run(self, cmd: str, expected_prompt: str = "# ", timeout_s: float = 5.0) -> str:
        """
        The ultimate command executor.
        Sends a command, eats the echo, and returns the output before the next prompt.
        """
        self.ser.reset_input_buffer()
        self.write_line(cmd)

        raw_output = self.expect(expected_prompt, timeout_s)

        # Clean the output (remove the echoed command and the trailing prompt)
        clean_lines = []
        for line in raw_output.split('\n'):
            clean_line = line.strip()
            if clean_line and clean_line != cmd and expected_prompt not in clean_line:
                clean_lines.append(clean_line)

        return "\n".join(clean_lines)

    # ==========================================
    # RESOURCE LOCKING (For Hardware Tests)
    # ==========================================
    @contextmanager
    def exclusive_raw_access(self) -> Generator[serial.Serial, None, None]:
        """
        Yields the raw PySerial object to a specific test (e.g., Loopback).
        Prevents the State Machine or background pollers from touching the port.
        """
        # THE FIX: Explicitly check `self.ser is None` to narrow the type for Pylance
        if not self.is_connected or self.ser is None:
            raise TransportConnectionError("Cannot grant exclusive access: UART port is completely closed.")

        logger.warning(f"[UART] Granting EXCLUSIVE raw binary access to port {self.cfg.port}")
        self._is_locked = True

        try:
            # Pylance is now completely satisfied that self.ser is not None
            yield self.ser
        finally:
            logger.debug("[UART] Revoking exclusive access. Restoring normal console mode.")
            self._is_locked = False
            if self.ser and self.ser.is_open:
                self.ser.reset_input_buffer()
