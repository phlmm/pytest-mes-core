# src/pytest_mes_core/transports/serial_client.py
import time
import re
import logging
import serial # type: ignore
from contextlib import contextmanager
from typing import Generator, Optional, Any

from pytest_mes_core.config import HostSerialConfig
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.utils.uart_parser import UartStreamParser

logger = logging.getLogger("mes_core.transports.serial")

class EphemeralSerialClient:
    """
    Unified Expect Engine for UART.
    Liskov-compliant with DutTransport. Handles EMI noise, ANSI stripping, and Exit Codes.
    """
    ANSI_ESCAPE_B = re.compile(br'\x1b\[[0-9;]*[a-zA-Z]')

    def __init__(self, cfg: HostSerialConfig):
        self.cfg = cfg
        self.ser: Optional[serial.Serial] = None
        self._is_locked = False
        self.parser = UartStreamParser()

    def connect(self) -> None:
        try:
            self.ser = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=self.cfg.timeout_s,
                exclusive=True
            )
        except serial.SerialException as e:
            raise TransportConnectionError(f"Failed to bind Host UART {self.cfg.port}: {e}")

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.close()

    @property
    def is_connected(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    def expect(self, pattern: str, timeout_s: float = 5.0, blast_char: str = "") -> str:
        if not self.is_connected or self.ser is None:
            raise TransportConnectionError("Serial port is closed.")
        if self._is_locked:
            raise RuntimeError("Cannot use expect() while UART is locked.")

        pattern_bytes = pattern.encode('utf-8')
        blast_bytes = blast_char.encode('utf-8') if blast_char else b""

        if blast_bytes:
            for _ in range(3):
                self.ser.write(blast_bytes)
                time.sleep(0.05)
            self.ser.flush()

        logger.debug(f"[UART] Expecting '{pattern}' (Timeout: {timeout_s}s)...")

        t_end = time.perf_counter() + timeout_s
        raw_buffer = bytearray()

        while time.perf_counter() < t_end:
            if self.ser.in_waiting > 0:
                raw_buffer.extend(self.ser.read(self.ser.in_waiting))
                clean_buffer = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)

                if pattern_bytes in clean_buffer:
                    return clean_buffer.decode('utf-8', errors='replace')

            time.sleep(0.01)

        dump = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)[-200:].decode('utf-8', errors='replace').strip()
        logger.error(f"[UART] Timeout expecting '{pattern}'. Buffer yielded: {dump}")
        raise TransportTimeoutError(f"UART Expect Timeout: '{pattern}' not found.")

    def write_line(self, cmd: str) -> None:
        if self._is_locked or self.ser is None:
            raise RuntimeError("Cannot write while UART is locked or closed.")
        logger.debug(f"[UART] TX -> '{cmd}'")
        self.ser.write(f"{cmd}\n".encode('utf-8'))
        self.ser.flush()

    # 🚨 SOTA FIX: Signature now perfectly matches DutTransport API
    def safe_run(self, cmd: str, timeout_s: float = 30.0, **kwargs: Any) -> CommandResult:
        """
        Executes a command and mathematically parses the exit code over a raw serial line.
        """
        expected_prompt = kwargs.get("expected_prompt", getattr(self.cfg, "os_shell_prompt", "~#"))
        check_exit_code = kwargs.get("check_exit_code", True)

        if not self.is_connected or self.ser is None:
            raise TransportConnectionError("Serial port is closed.")

        self.ser.reset_input_buffer()
        t0 = time.perf_counter()

        # Fast wakeup pulse bypass (sent by Failover router)
        if not cmd.strip():
            self.ser.write(cmd.encode('utf-8'))
            self.ser.flush()
            try:
                self.expect(expected_prompt, timeout_s=timeout_s)
            except TransportTimeoutError:
                pass
            duration = round(time.perf_counter() - t0, 3)
            return CommandResult(command=cmd, stdout="", stderr="", exited=0, ok=True, duration_s=duration)

        is_uboot = any(p in expected_prompt for p in ["=>", "U-Boot", "barebox", "Verdin"])

        # 🚨 SOTA Trick: Inject an echo to parse the actual Linux exit code over serial!
        if check_exit_code and not is_uboot:
            magic_delim = "MES_EXIT_CODE:"
            injected_cmd = f"{cmd} ; echo {magic_delim}$?"
            self.write_line(injected_cmd)
        else:
            injected_cmd = cmd
            self.write_line(cmd)

        try:
            raw_output = self.expect(expected_prompt, timeout_s)
            duration = round(time.perf_counter() - t0, 3)

            clean_lines = []
            exited = 0 if check_exit_code else 0

            for line in raw_output.split('\n'):
                clean = line.strip()
                # Strip echoed command and prompt
                if not clean or clean == cmd or clean == injected_cmd or expected_prompt in clean:
                    continue

                if check_exit_code and not is_uboot and "MES_EXIT_CODE:" in clean:
                    try:
                        exited = int(clean.split("MES_EXIT_CODE:")[1])
                    except ValueError:
                        exited = -1
                    continue

                clean_lines.append(clean)

            stdout = "\n".join(clean_lines)

            # Heuristic failure fallback for U-Boot since we can't echo $?
            if is_uboot and ("Unknown command" in stdout or "Error" in stdout):
                exited = 1

            return CommandResult(
                command=cmd,
                stdout=stdout,
                stderr="", # UART physically multiplexes stderr into stdout
                exited=exited,
                ok=(exited == 0),
                duration_s=duration
            )

        except TransportTimeoutError as e:
            duration = round(time.perf_counter() - t0, 3)
            logger.warning(f"[UART] Execution timed out after {timeout_s}s: {cmd}")
            return CommandResult(
                command=cmd,
                stdout=self.live_buffer,
                stderr=str(e),
                exited=-1,
                ok=False,
                duration_s=duration
            )

    def flush_buffers(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.reset_input_buffer()
        self.parser.clear_buffer()

    def read_clean_stream(self) -> Generator[str, None, None]:
        if not self.ser or not self.ser.is_open: return
        if self.ser.in_waiting > 0:
            raw_bytes = self.ser.read(self.ser.in_waiting)
            self.parser.ingest(raw_bytes)
        yield from self.parser.extract_lines()

    @property
    def live_buffer(self) -> str:
        return self.parser.buffer

    @contextmanager
    def exclusive_raw_access(self) -> Generator[serial.Serial, None, None]:
        if not self.is_connected or self.ser is None:
            raise TransportConnectionError("Cannot grant exclusive access: UART port is closed.")
        logger.warning(f"[UART] Granting EXCLUSIVE raw binary access to port {self.cfg.port}")
        self._is_locked = True
        try:
            yield self.ser
        finally:
            logger.debug("[UART] Revoking exclusive access.")
            self._is_locked = False
            if self.ser and self.ser.is_open:
                self.ser.reset_input_buffer()
