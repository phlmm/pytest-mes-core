# src/pytest_mes_core/transports/serial_client.py
import time
import re
import uuid
import logging
import serial # type: ignore
from contextlib import contextmanager
from typing import Generator, Optional, Any

from pytest_mes_core.config import HostSerialConfig
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.utils.uart_parser import UartStreamParser
from pytest_mes_core.transports.watchdog import UartKernelWatchdog

logger = logging.getLogger("mes_core.transports.serial")

class EphemeralSerialClient:
    """
    Unified Expect Engine for UART.
    Liskov-compliant with DutTransport. Handles EMI noise, ANSI stripping,
    asynchronous kernel logs, and strict OS-level IO blocking.
    """
    # Matches ANSI color codes, cursor movements, and terminal clear commands
    ANSI_ESCAPE_B = re.compile(br'\x1b\[[0-9;]*[a-zA-Z]')

    # Matches Linux kernel timestamps like: "[  123.456789] usb disconnect"
    KERNEL_LOG_PATTERN = re.compile(r'^\[\s*\d+\.\d+\]\s*')

    def __init__(self, cfg: HostSerialConfig):
        self.cfg = cfg
        self.ser: Optional[serial.Serial] = None
        self._is_locked = False
        self._is_executing = False
        self.parser = UartStreamParser()
        self.watchdog = UartKernelWatchdog(self)

    def connect(self) -> None:
        try:
            self.ser = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=0.1,  # Short block for efficient OS-level I/O multiplexing
                exclusive=True
            )
            self.flush_buffers()
            logger.debug(f"[UART] Bound to {self.cfg.port} and flushed stale OS buffers.")
            self.watchdog.start()
        except serial.SerialException as e:
            raise TransportConnectionError(f"Failed to bind Host UART {self.cfg.port}: {e}")

    def disconnect(self) -> None:
        self.watchdog.stop()
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
        if blast_char:
            blast_bytes = blast_char.encode('utf-8')
            for _ in range(3):
                self.ser.write(blast_bytes)
                time.sleep(0.05)
            self.ser.flush()

        logger.debug(f"[UART] Expecting '{pattern}' (Timeout: {timeout_s}s)...")
        t_end = time.perf_counter() + timeout_s
        last_rx_time = time.perf_counter()
        raw_buffer = bytearray()

        self._is_executing = True
        try:
            while time.perf_counter() < t_end:
                if self.ser.in_waiting > 0:
                    chunk = self.ser.read(max(1, self.ser.in_waiting))
                raw_buffer.extend(chunk)

                # Strip ANSI codes live to prevent prompt obfuscation
                clean_buffer = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)

                if pattern_bytes in clean_buffer:
                    return clean_buffer.decode('utf-8', errors='replace')

                last_rx_time = time.perf_counter()
            else:
                # ACTIVE PINGING: If the console is silent for 2s, the prompt may have been split
                # by a kernel log. Inject a newline to force the OS to cleanly redraw the prompt.
                if time.perf_counter() - last_rx_time > 2.0:
                    logger.debug("[UART] Console silent. Injecting heartbeat to redraw prompt...")
                    try:
                        self.ser.write(b'\n')
                        self.ser.flush()
                    except serial.SerialException:
                        pass
                    last_rx_time = time.perf_counter()

                time.sleep(0.01) # Yield to prevent CPU thrashing
        finally:
            self._is_executing = False

        # Timeout occurred
        dump = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)[-200:].decode('utf-8', errors='replace').strip()
        logger.error(f"[UART] Timeout expecting '{pattern}'. Buffer yielded: {dump}")
        raise TransportTimeoutError(f"UART Expect Timeout: '{pattern}' not found.")

    def write_line(self, cmd: str, sensitive: bool = False) -> None:
        if self._is_locked or self.ser is None:
            raise RuntimeError("Cannot write while UART is locked or closed.")

        if sensitive:
            logger.debug("[UART] TX -> '********'")
        else:
            # Truncate massive base64 payloads in the debug trace
            log_cmd = cmd if len(cmd) < 256 else cmd[:253] + "..."
            logger.debug(f"[UART] TX -> '{log_cmd}'")

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
        expected_prompt = kwargs.get("expected_prompt", getattr(self.cfg, "os_shell_prompt", "~#"))

        if not self.is_connected or self.ser is None:
            raise TransportConnectionError("Serial port is closed.")

        self.flush_buffers()
        t0 = time.perf_counter()

        # Fast wakeup pulse bypass (sent by Failover router)
        if not cmd.strip():
            self.write_line("")
            try:
                self.expect(expected_prompt, timeout_s=1.0)
            except TransportTimeoutError:
                pass
            duration = round(time.perf_counter() - t0, 3)
            return CommandResult(command=cmd, stdout="", stderr="", exited=0, ok=True, duration_s=duration)

        is_uboot = any(p in expected_prompt for p in ["=>", "U-Boot", "barebox", "Verdin"])

        # ==========================================
        # ROBUST FRAMED PAYLOAD INJECTION
        # ==========================================
        if not is_uboot:
            # 1. Generate a cryptographic UUID to defend against ghost echoes
            exec_token = uuid.uuid4().hex[:8]
            magic_marker = f"__MES_EXIT_{exec_token}__"

            # 2. Escape single quotes safely for the subshell wrapper
            safe_cmd = cmd.replace("'", "'\\''")

            # 3. Wrap the command in `sh -c` to protect background operators (&, ||, &&)
            # 4. Use `printf` for an atomic TTY write to prevent kernel printk interleaving
            injected_cmd = f"sh -c '{safe_cmd}' ; printf '\\n{magic_marker}:%d\\n' $?"
        else:
            injected_cmd = cmd

        self.write_line(injected_cmd)

        try:
            raw_output = self.expect(expected_prompt, timeout_s)
            duration = round(time.perf_counter() - t0, 3)

            # --- Robust Output Parsing ---
            stdout = raw_output
            exited = -1 if is_uboot else 0

            if not is_uboot:
                # Extract the exit code even if surrounded by kernel panics
                exit_match = re.search(fr"{magic_marker}:(\d+)", stdout)
                if exit_match:
                    exited = int(exit_match.group(1))
                    # Surgically remove the magic token from the final output
                    stdout = stdout.replace(exit_match.group(0), "")
                else:
                    # If the token is entirely missing, the shell crashed or the board rebooted
                    exited = -2

            # Clean up command echo, kernel spam, and prompt
            clean_lines = []
            for line in stdout.split('\n'):
                clean = line.strip()

                # 1. Strip empty lines and the OS shell prompt
                if not clean or expected_prompt in clean:
                    continue

                # 2. Strip echoed command artifacts
                if clean == cmd or clean == injected_cmd or clean.startswith("sh -c '"):
                    continue

                # 3. Strip asynchronous kernel dmesg spam (e.g., "[  14.432] eth0: link up")
                if self.KERNEL_LOG_PATTERN.search(clean):
                    logger.debug(f"[UART] Suppressed async kernel log: {clean}")
                    continue

                clean_lines.append(clean)

            stdout_clean = "\n".join(clean_lines).strip()

            # U-Boot heuristic failure fallback
            if is_uboot and ("Unknown command" in stdout_clean or "Error" in stdout_clean):
                exited = 1
            elif is_uboot:
                exited = 0

            result = CommandResult(
                command=cmd,
                stdout=stdout_clean,
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
        """Purges both the OS-level UART FIFO and our internal string buffer."""
        if self.ser and self.ser.is_open:
            self.ser.reset_input_buffer()
        self.parser.clear_buffer()

    def read_clean_stream(self) -> Generator[str, None, None]:
        """Provides a live, ANSI-stripped generator for real-time log trailing (e.g., UUU/TEZI)."""
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
        """Temporarily yields raw OS socket control for deep hardware flashes (e.g., uuu)."""
        if not self.is_connected or self.ser is None:
            raise TransportConnectionError("Cannot grant exclusive access: UART port is closed.")

        logger.warning(f"[UART] Granting EXCLUSIVE raw binary access to port {self.cfg.port}")
        self._is_locked = True
        try:
            yield self.ser
        finally:
            logger.debug("[UART] Revoking exclusive access.")
            self._is_locked = False
            self.flush_buffers()
