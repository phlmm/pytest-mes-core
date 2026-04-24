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
        """Binds to the host UART port with exclusive OS locking and flushes stale buffers.

        Raises:
            TransportConnectionError: If the port fails to bind or is locked by another process.
        """
        try:
            self.ser = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=0.1,  # Short block for efficient OS-level I/O multiplexing
                exclusive=True
            )
            # Purge both FIFOs: any stale TX bytes left in the output queue
            # by a previous session would appear as spurious RX to the next
            # opener (e.g. TIO).  reset_output_buffer() drops them before the
            # driver ever tries to transmit them.
            self.ser.reset_output_buffer()
            self.flush_buffers()
            logger.debug(f"[UART] Bound to {self.cfg.port} and flushed stale OS buffers (TX+RX).")
            self.watchdog.start()
        except serial.SerialException as e:
            err_str = str(e).lower()
            if "device or resource busy" in err_str or "access is denied" in err_str:
                from pytest_mes_core.host_adapters.diagnostics import ResourceDiagnostics
                owner = ResourceDiagnostics.get_device_owner(self.cfg.port)
                logger.critical("="*60)
                if owner:
                    error_msg = f"Serial port {self.cfg.port} is locked by PID/Process: {owner}!"
                    logger.critical(f"[UART] FATAL: {error_msg}")
                    logger.critical("[UART] Please close the competing application (minicom, Putty) and retry.")
                else:
                    error_msg = f"Serial port {self.cfg.port} is busy (OS refused to identify owner)."
                    logger.critical(f"[UART] FATAL: {error_msg}")
                logger.critical("="*60)
                raise TransportConnectionError(error_msg)
            raise TransportConnectionError(f"Failed to bind Host UART {self.cfg.port}: {e}")

    def disconnect(self) -> None:
        """Safely tears down the UART interface, stops the watchdog, and releases the OS lock.

        Flushes both the input and output OS FIFOs before closing the file
        descriptor.  Without this, any bytes that pyserial has queued in the
        OS TX buffer but not yet transmitted remain in the UART driver and will
        be seen as spurious RX by the next process that opens the port (e.g.
        TIO, minicom).
        """
        self.watchdog.stop()
        if self.ser and self.ser.is_open:
            try:
                self.ser.reset_output_buffer()
                self.ser.reset_input_buffer()
            except Exception:
                pass  # Port may have already become inaccessible (USB unplug)
            self.ser.close()

    @property
    def is_connected(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    @contextmanager
    def execution_lock(self) -> Generator[None, None, None]:
        """Temporarily pauses watchdog byte-stealing without killing the thread."""
        self._is_executing = True
        try:
            yield
        finally:
            self._is_executing = False

    def expect(self, pattern: str, timeout_s: float = 5.0, blast_char: str = "", active_redraw: bool = True) -> str:
        """
        Blocks until ``pattern`` appears in the UART stream or ``timeout_s`` elapses.

        Args:
            pattern: The string to wait for (plain text, not regex).
            timeout_s: Maximum seconds to wait before raising TransportTimeoutError.
            blast_char: Optional character to write N times before starting to listen
                        (e.g. '\n' to wake a sleeping shell).
            active_redraw: When True (default), injects a newline if the console has been
                           silent for 2s. This forces the OS to redraw a partially-written
                           login prompt that was buried by async kernel dmesg spam.
                           Set to False when listening passively through U-Boot autoboot
                           to avoid accidentally halting the countdown.
        """
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

        logger.debug(f"[UART] Expecting '{pattern}' (Timeout: {timeout_s}s, active_redraw={active_redraw})...")
        t_end = time.perf_counter() + timeout_s
        last_rx_time = time.perf_counter()
        raw_buffer = bytearray()

        with self.execution_lock():
            while time.perf_counter() < t_end:
                chunk = b""
                if self.ser.in_waiting > 0:
                    chunk = self.ser.read(max(1, self.ser.in_waiting))
                raw_buffer.extend(chunk)

                # Strip ANSI codes live to prevent prompt obfuscation
                clean_buffer = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)

                if pattern_bytes in clean_buffer:
                    return clean_buffer.decode('utf-8', errors='replace')

                # Only refresh the silence timer when bytes actually arrived.
                # BUG WAS HERE: unconditionally updating last_rx_time on every
                # loop tick meant the 2-second threshold was unreachable.
                if chunk:
                    last_rx_time = time.perf_counter()

                if active_redraw and (time.perf_counter() - last_rx_time > 2.0):
                    # Console is silent and a kernel log may have buried the prompt.
                    # Inject an active ping to force the OS to redraw it immediately.
                    logger.debug("[UART] Console silent. Injecting ping to redraw prompt...")
                    self.ser.write(b"\n")
                    self.ser.flush()
                    last_rx_time = time.perf_counter()

                time.sleep(0.01)

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

        payload = f"{cmd}\n".encode('utf-8')
        for i in range(0, len(payload), 16):
            self.ser.write(payload[i:i+16])
            self.ser.flush()
            time.sleep(0.002)

    def safe_run(
        self,
        cmd: str,
        timeout_s: float = 30.0,
        check_exit_code: bool = False,
        auto_retry: bool = False,
        **kwargs: Any
    ) -> CommandResult:
        """Executes a command synchronously over the serial UART interface.

        Uses robust framed payload injection to isolate command output from kernel spam
        and shell echoes.

        Args:
            cmd: The shell command to execute.
            timeout_s: Maximum seconds to wait before timing out.
            check_exit_code: If True, raises RuntimeError on non-zero exit code.
            auto_retry: If True, indicates the command is idempotent (handled by Failover router).
            **kwargs: Additional options like 'expected_prompt'.

        Returns:
            CommandResult: The parsed, immutable command outcome.

        Raises:
            TransportConnectionError: If the port is disconnected.
            RuntimeError: If check_exit_code is True and the command fails or times out.
        """
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
            start_marker = f"__MES_START_{exec_token}__"

            # 2. Escape single quotes safely for the subshell wrapper
            safe_cmd = cmd.replace("'", "'\\''")

            # 3. Wrap the command in `sh -c` to protect background operators (&, ||, &&)
            # 4. Use `printf` for atomic markers to isolate execution output from echoed characters
            injected_cmd = f"printf '\\n{start_marker}\\n' ; sh -c '{safe_cmd}' ; printf '\\n{magic_marker}:%d\\n' $?"
        else:
            injected_cmd = cmd

        # Send Ctrl+C to abort any half-typed command left over from previous failures
        self.ser.write(b'\x03')
        self.ser.flush()
        try:
            # Actively wait for the shell to redraw the prompt so it's ready to accept input
            self.expect(expected_prompt, timeout_s=0.5)
        except TransportTimeoutError:
            pass
        self.flush_buffers()

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
                else:
                    exited = -2

                # Isolate the exact execution output between the start and exit markers
                end_idx = stdout.rfind(magic_marker)
                if end_idx != -1:
                    stdout = stdout[:end_idx]
                else:
                    stdout = stdout.replace(magic_marker, "")

                start_idx = stdout.rfind(start_marker)
                if start_idx != -1:
                    stdout = stdout[start_idx + len(start_marker):]

            # Clean up command echo, kernel spam, and prompt
            clean_lines = []
            for line in stdout.split('\n'):
                clean = line.strip()

                # 1. Strip empty lines and the OS shell prompt
                if not clean or expected_prompt in clean:
                    continue

                # 2. Strip asynchronous kernel dmesg spam (e.g., "[  14.432] eth0: link up")
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
        """Purges both the OS-level UART FIFO and our internal string buffer.
        
        Uses a drain-loop strategy to mitigate USB-Serial hardware FIFO ghosts
        where bytes might still be in transit across the USB bus after reset.
        """
        if self.ser and self.ser.is_open:
            self.ser.reset_output_buffer()
            self.ser.reset_input_buffer()
            
            # Allow any in-flight USB bulk packets to arrive at the host
            time.sleep(0.05)
            
            # Physically drain the FIFO if the hardware pushed late data
            while self.ser.in_waiting > 0:
                self.ser.read(max(1, self.ser.in_waiting))
                
        self.parser.clear_buffer()

    # ------------------------------------------------------------------
    # RAW PORT ACCESSORS
    # Used exclusively by low-level boot-detection loops in the FSM.
    # No other caller should access .ser directly.
    # ------------------------------------------------------------------

    def raw_write(self, data: bytes) -> None:
        """Write raw bytes to the port and flush. Guards against None/closed port."""
        if self.ser and self.ser.is_open:
            self.ser.write(data)
            self.ser.flush()

    def raw_read_pending(self) -> int:
        """Returns the number of bytes waiting in the OS receive buffer."""
        return self.ser.in_waiting if self.ser and self.ser.is_open else 0

    def raw_read_chunk(self) -> bytes:
        """Reads all pending bytes without blocking. Returns b'' if nothing available."""
        n = self.raw_read_pending()
        return self.ser.read(n) if n > 0 else b""

    def raw_set_timeout(self, timeout: float) -> None:
        """Adjusts the OS-level read timeout. Boot loops switch between 0 (non-blocking)
        and >0 (blocking) at specific phases; encapsulating this avoids bare .ser access.
        """
        if self.ser and self.ser.is_open:
            self.ser.timeout = timeout


    def read_clean_stream(self, filter_kernel: bool = True) -> Generator[str, None, None]:
        """Provides a live, ANSI-stripped generator for real-time log trailing (e.g., UUU/TEZI).

        Guards the underlying read with execution_lock so the watchdog thread
        cannot steal bytes from the OS FIFO simultaneously (data race fix).
        """
        if not self.ser or not self.ser.is_open:
            return
        with self.execution_lock():
            if self.ser.in_waiting > 0:
                raw_bytes = self.ser.read(self.ser.in_waiting)
                self.parser.ingest(raw_bytes)

        for line in self.parser.extract_lines():
            if filter_kernel and self.KERNEL_LOG_PATTERN.match(line):
                continue
            yield line

    @property
    def live_buffer(self) -> str:
        return self.parser.buffer

    @contextmanager
    def exclusive_raw_access(self) -> Generator[serial.Serial, None, None]:
        """Temporarily yields raw OS socket control for deep hardware flashes (e.g., uuu).

        The kernel watchdog background thread is explicitly stopped for the duration.
        A flag-based approach has a TOCTOU window: the watchdog could pass its
        _is_locked check and then read from the FD simultaneously. Stopping the
        thread is safe because exclusive access is always a well-bounded operation.
        """
        if not self.is_connected or self.ser is None:
            raise TransportConnectionError("Cannot grant exclusive access: UART port is closed.")

        logger.warning(f"[UART] Granting EXCLUSIVE raw binary access to port {self.cfg.port}")
        self.watchdog.stop()
        self._is_locked = True
        try:
            yield self.ser
        finally:
            logger.debug("[UART] Revoking exclusive access. Restarting kernel watchdog.")
            self._is_locked = False
            self.flush_buffers()
            self.watchdog.start()
