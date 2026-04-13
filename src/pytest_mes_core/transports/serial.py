import re
import time
import threading
import logging
import serial  # type: ignore
from typing import Dict, Optional, Any, List, Union

from pytest_mes_core.config import BootProfilerConfig, HostSerialConfig
from pytest_mes_core.transports.base import (
    CommandResult,
    TransportConnectionError,
    TransportTimeoutError
)

logger = logging.getLogger("mes_core.transports.serial")

# ==========================================
# BACKGROUND PROFILER
# ==========================================

class AsyncBootProfiler:
    """Asynchronous Regex-based UART monitor driven entirely by TOML config."""

    def __init__(self, cfg: BootProfilerConfig):
        self.cfg = cfg

        # 1. Normalize the milestones to guarantee strict JSONL keys
        normalized_milestones = self._normalize_milestones(cfg.milestones)

        # 2. Precompile regexes immediately
        self.targets = {name: re.compile(pattern) for name, pattern in normalized_milestones.items()}

        # 3. Pre-fill metrics with -1.0 to enforce strict JSONL schema output
        self.metrics: Dict[str, float] = {name: -1.0 for name in self.targets.keys()}

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _normalize_milestones(self, milestones: Union[Dict[str, str], List[str]]) -> Dict[str, str]:
        """
        Defensive Normalization:
        If the user provides a list of raw strings, this converts them into safe,
        Grafana-compliant metric keys (e.g., "U-Boot SPL" -> "t_u_boot_spl")
        """
        if isinstance(milestones, dict):
            return milestones

        normalized: Dict[str, str] = {}
        for pattern in milestones:
            # Lowercase, replace non-alphanumeric with underscores, strip trailing underscores
            safe_slug = re.sub(r'[^a-z0-9]+', '_', pattern.lower()).strip('_')

            # Enforce 't_' prefix for time-based metrics
            safe_key = f"t_{safe_slug}"
            normalized[safe_key] = pattern

        return normalized

    def start_profiling(self, t_zero_perf: float) -> None:
        self.metrics["t_power_on"] = 0.0
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen, args=(t_zero_perf,), daemon=True)
        self._thread.start()
        logger.info(f"[Profiler] Armed on {self.cfg.port} @ {self.cfg.baudrate}bps")

    def _listen(self, t_zero: float) -> None:
        pending = self.targets.copy()

        try:
            with serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=0.1) as ser:
                ser.reset_input_buffer()

                while not self._stop_event.is_set() and pending:
                    line = ser.readline().decode('utf-8', errors='replace').strip()
                    if not line: continue

                    current_time = round(time.perf_counter() - t_zero, 3)

                    hit_keys = []
                    for name, regex in pending.items():
                        if regex.search(line):
                            self.metrics[name] = current_time
                            hit_keys.append(name)
                            logger.debug(f"[Profiler] Milestone '{name}' hit at {current_time}s")

                    for k in hit_keys:
                        del pending[k]

        except serial.SerialException as e:
            logger.error(f"[Profiler] FATAL: UART hardware disconnected: {e}")
            self.metrics["profiler_error"] = -1.0

    def stop_and_fetch(self) -> Dict[str, float]:
        """Returns recorded timestamps using the TOML-configured timeout."""
        logger.debug("[Profiler] Halting UART listener...")
        self._stop_event.set()

        if self._thread:
            self._thread.join(timeout=self.cfg.timeout_s)
            if self._thread.is_alive():
                logger.critical(f"[Profiler] UART Thread refused to die on {self.cfg.port}!")

        missed = [k for k, v in self.metrics.items() if v == -1.0 and k != "profiler_error"]
        if missed:
            logger.warning(f"[Profiler] Missed milestones: {missed}")

        return self.metrics

# ==========================================
# PHYSICAL TRANSPORT CONTRACT
# ==========================================
class EphemeralSerialClient:
    """
    Bare-metal UART transport layer.
    Navigates U-Boot and Linux prompts without an active IP stack.
    Fully implements the rigorous DutTransport Protocol.
    """
    def __init__(self, cfg: HostSerialConfig, expected_prompt: str = "# "):
        self.cfg = cfg
        self.prompt = expected_prompt.encode('utf-8')
        self.conn: Optional[serial.Serial] = None

    # ------------------------------------------
    # LIFECYCLE MANAGEMENT
    # ------------------------------------------
    @property
    def is_connected(self) -> bool:
        """Required by DutTransport Contract."""
        return self.conn is not None and self.conn.is_open

    def connect(self) -> None:
        """Initializes hardware interface and achieves prompt sync."""
        if self.is_connected:
            return

        logger.debug(f"[Serial] Opening physical UART on {self.cfg.port} at {self.cfg.baudrate} baud...")
        try:
            self.conn = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=self.cfg.timeout_s
            )
            # Flush any garbage left over from previous E-Stops or crashes
            self.conn.reset_input_buffer()
            self.conn.reset_output_buffer()

            # Send a blank carriage return to force the DUT to print its prompt
            self.conn.write(b"\r\n")
            self._read_until_prompt(timeout_s=2.0)
            logger.info(f"[Serial] Transport established on {self.cfg.port}.")

        except serial.SerialException as e:
            logger.critical(f"[Serial] Physical hardware error on {self.cfg.port}: {e}")
            raise TransportConnectionError(f"Failed to open UART transport: {e}")

    def disconnect(self) -> None:
        """Zero-Leakage teardown (Required by DutTransport Contract)."""
        if self.conn and self.conn.is_open:
            logger.debug(f"[Serial] Closing UART transport on {self.cfg.port}.")
            self.conn.close()

    # ------------------------------------------
    # COMMAND EXECUTION
    # ------------------------------------------
    def safe_run(self, cmd: str, timeout_s: float = 30.0, check_exit_code: bool = True) -> CommandResult:
        """
        Executes a command and forces the remote shell to return an exit code.
        Drops echoed text and isolates the pure stdout.
        """
        if not self.is_connected or not self.conn:
            raise TransportConnectionError("Cannot execute: Serial port is closed.")

        logger.debug(f"[Serial TX] {cmd}")
        t0 = time.perf_counter()

        try:
            self.conn.reset_input_buffer()
            # 1. Transmit
            self.conn.write(f"{cmd}\n".encode('utf-8'))
            self.conn.flush()

            # 2. Receive Output
            raw_output = self._read_until_prompt(timeout_s)
            cleaned_stdout = self._clean_output(raw_output.decode('utf-8', errors='replace'), cmd)

            # 3. Retrieve Exit Code (Linux Only)
            exit_code = 0
            if check_exit_code:
                self.conn.write(b"echo $?\n")
                rc_raw = self._read_until_prompt(timeout_s=2.0).decode('utf-8', errors='replace')
                rc_clean = self._clean_output(rc_raw, "echo $?").strip()

                # Robust Regex Parsing (Defends against ANSI escape codes)
                match = re.search(r'\d+', rc_clean)
                exit_code = int(match.group()) if match else -1

        except serial.SerialException as e:
            # THE SURVIVAL EVENT: USB cable unplugged or kernel EMI drop
            self.disconnect()
            raise TransportConnectionError(f"Serial pipe shattered during execution: {e}")

        # 4. Enforce Immutability & Telemetry tracking
        duration = round(time.perf_counter() - t0, 3)
        is_ok = (exit_code == 0)

        # UART doesn't separate stderr. If it failed, we assume all output is error context.
        return CommandResult(
            command=cmd,
            stdout=cleaned_stdout if is_ok else "",
            stderr=cleaned_stdout if not is_ok else "",
            exited=exit_code,
            ok=is_ok,
            duration_s=duration
        )

    # ------------------------------------------
    # INTERNAL HELPERS
    # ------------------------------------------
    def _read_until_prompt(self, timeout_s: float) -> bytes:
        """
        High-performance, C-optimized read.
        Raises Domain Exceptions if the timeout expires or the pipe breaks.
        """
        if not self.conn:
            raise TransportConnectionError("Connection is None.")

        self.conn.timeout = timeout_s
        try:
            # Use PySerial's native, highly optimized read_until
            raw_bytes = self.conn.read_until(expected=self.prompt)

            if not raw_bytes.endswith(self.prompt):
                raise TransportTimeoutError(f"Serial timeout ({timeout_s}s) waiting for prompt.")

            return raw_bytes

        except serial.SerialException as e:
            raise TransportConnectionError(f"Hardware interrupt during read: {e}")

    def _clean_output(self, raw_text: str, sent_cmd: str) -> str:
        """Strips the echoed command and the trailing prompt from the terminal output."""
        lines = raw_text.replace('\r\n', '\n').split('\n')

        # Strip the echoed command at the top
        if lines and sent_cmd in lines[0]:
            lines = lines[1:]

        # Join and strip the prompt at the bottom
        cleaned = '\n'.join(lines).replace(self.prompt.decode('utf-8'), '').strip()
        return cleaned
