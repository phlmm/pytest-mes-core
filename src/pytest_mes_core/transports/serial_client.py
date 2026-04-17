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
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self.parser.clear_buffer()
            logger.debug(f"[UART] Bound to {self.cfg.port} and flushed stale OS buffers.")
        except serial.SerialException as e:
            raise TransportConnectionError(f"Failed to bind Host UART {self.cfg.port}: {e}")

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            logger.debug(f"[UART] Disconnecting {self.cfg.port}. Flushing residual buffers...")
            self.flush_buffers() # FIX: Purge all hardware caches before relinquishing the port
            self.ser.close()

    @property
    def is_connected(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    def expect(
        self,
        pattern: str,
        timeout_s: float = 5.0,
        blast_char: str = "",
        active_redraw: bool = True
    ) -> str:
        """
        Active Hunter Expect Engine.
        Scans for regex patterns while dynamically hitting [ENTER] to rescue buried prompts.
        """
        if not self.is_connected or self.ser is None:
            raise TransportConnectionError("Serial port is closed.")
        if self._is_locked:
            raise RuntimeError("Cannot use expect() while UART is locked.")

        # Upgrade to Regex matching for highly flexible parsing
        search_regex = re.compile(pattern)
        blast_bytes = blast_char.encode('utf-8') if blast_char else b""

        if blast_bytes:
            for _ in range(3):
                self.ser.write(blast_bytes)
                time.sleep(0.05)
            self.ser.flush()

        logger.debug(f"[UART] Expecting '{pattern}' (Timeout: {timeout_s}s)...")

        t_end = time.perf_counter() + timeout_s
        last_redraw_time = time.perf_counter()
        raw_buffer = bytearray()

        try:
            while time.perf_counter() < t_end:
                # 1. Ingest available bytes
                if self.ser.in_waiting > 0:
                    raw_buffer.extend(self.ser.read(self.ser.in_waiting))
                    clean_buffer = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)
                    decoded_buffer = clean_buffer.decode('utf-8', errors='replace')

                    # 2. Regex search
                    if search_regex.search(decoded_buffer):
                        return decoded_buffer

                # 3. Active Redraw Mechanism
                now = time.perf_counter()
                if active_redraw and (now - last_redraw_time) > 1.5:
                    self.ser.write(b"\n")
                    self.ser.flush()
                    last_redraw_time = now

                time.sleep(0.05)

        except KeyboardInterrupt:
            # 🚨 THE FIX: Catch the user pressing Ctrl+C mid-wait
            logger.warning(f"\n[UART] ⚠️ Ctrl+C Detected! Force-flushing hardware buffers on {self.cfg.port} before aborting...")
            self.flush_buffers()
            raise

        # 4. Timeout Failure Formatting
        dump = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)[-200:].decode('utf-8', errors='replace').strip()
        logger.error(f"[UART] Timeout expecting '{pattern}'. Buffer yielded: {dump}")
        raise TransportTimeoutError(f"UART Expect Timeout: '{pattern}' not found.")

    def write_line(self, cmd: str) -> None:
        if self._is_locked or self.ser is None:
            raise RuntimeError("Cannot write while UART is locked or closed.")
        logger.debug(f"[UART] TX -> '{cmd}'")
        self.ser.write(f"{cmd}\n".encode('utf-8'))
        self.ser.flush()

    def safe_run(
        self,
        cmd: str,
        timeout_s: float = 30.0,
        check_exit_code: bool = False,
        auto_retry: bool = False,
        **kwargs: Any
    ) -> CommandResult:
        """
        Executes a command and mathematically parses the exit code over a raw serial line.
        """
        expected_prompt = kwargs.get("expected_prompt", getattr(self.cfg, "os_shell_prompt", "~#"))

        if not self.is_connected or self.ser is None:
            raise TransportConnectionError("Serial port is closed.")

        self.ser.reset_input_buffer()
        t0 = time.perf_counter()

        # Fast wakeup pulse bypass (sent by Failover router)
        if not cmd.strip():
            self.ser.write(cmd.encode('utf-8'))
            self.ser.flush()
            try:
                self.expect(expected_prompt, timeout_s=timeout_s, active_redraw=False)
            except TransportTimeoutError:
                pass
            duration = round(time.perf_counter() - t0, 3)
            return CommandResult(command=cmd, stdout="", stderr="", exited=0, ok=True, duration_s=duration)

        is_uboot = any(p in expected_prompt for p in ["=>", "U-Boot", "barebox", "Verdin"])

        # SOTA Trick: Always inject the echo so we can build an accurate CommandResult
        if not is_uboot:
            magic_delim = "MES_EXIT_CODE:"
            injected_cmd = f"{cmd} ; echo {magic_delim}$?"
            self.write_line(injected_cmd)
        else:
            injected_cmd = cmd
            self.write_line(cmd)

        try:
            # Active redraw is safe here because safe_run expects the shell to return.
            raw_output = self.expect(expected_prompt, timeout_s, active_redraw=True)
            duration = round(time.perf_counter() - t0, 3)

            clean_lines = []
            exited = -1 if is_uboot else 0

            for line in raw_output.split('\n'):
                clean = line.strip()

                # Strip echoed command and prompt
                if not clean or clean == cmd or clean == injected_cmd or expected_prompt in clean:
                    continue

                if not is_uboot and "MES_EXIT_CODE:" in clean:
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
            elif is_uboot:
                exited = 0

            result = CommandResult(
                command=cmd,
                stdout=stdout,
                stderr="", # UART physically multiplexes stderr into stdout
                exited=exited,
                ok=(exited == 0),
                duration_s=duration
            )

            if check_exit_code and not result.ok:
                raise RuntimeError(f"UART Command '{cmd}' failed with exit code {result.exited}:\n{result.stdout}")

            return result

        except TransportTimeoutError as e:
            duration = round(time.perf_counter() - t0, 3)
            logger.warning(f"[UART] Execution timed out after {timeout_s}s: {cmd}")

            result = CommandResult(
                command=cmd,
                stdout=self.live_buffer,
                stderr=str(e),
                exited=-1,
                ok=False,
                duration_s=duration
            )

            if check_exit_code:
                raise RuntimeError(f"UART Command '{cmd}' timed out after {timeout_s}s")

            return result

    def flush_buffers(self) -> None:
        """Aggressively flushes OS-level hardware buffers and internal software buffers."""
        if self.ser and self.ser.is_open:
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
            except Exception:
                pass # Ignore if the USB cable was physically yanked
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
