# src/pytest_mes_core/transports/serial.py
import sys
import re
import time
import threading
import logging
import serial  # type: ignore
from typing import Dict, Optional, Any, List, Union

from pytest_mes_core.config import BootProfilerConfig, HostSerialConfig
from pytest_mes_core.transports import (
    CommandResult,
    TransportConnectionError,
    TransportTimeoutError
)

logger = logging.getLogger("mes_core.transports.serial")

# ==========================================
# BACKGROUND PROFILER
# ==========================================
class LiveBootProfiler:
    """
    State machine that consumes lines from the RX Daemon.
    Calculates exact boot timing milestones dynamically.
    """
    def __init__(self, cfg: BootProfilerConfig):
        self.cfg = cfg
        self._t_zero = 0.0

        # Normalize milestones (e.g. "U-Boot 2022" -> "t_u_boot_2022")
        self._normalized_names = self._normalize_milestones(cfg.milestones)
        self.targets: Dict[str, re.Pattern] = {}
        self.metrics: Dict[str, float] = {}

    def _normalize_milestones(self, milestones: Union[Dict[str, str], List[str]]) -> Dict[str, str]:
        if isinstance(milestones, dict):
            return milestones
        normalized = {}
        for pattern in milestones:
            safe_slug = re.sub(r'[^a-z0-9]+', '_', pattern.lower()).strip('_')
            normalized[f"t_{safe_slug}"] = pattern
        return normalized

    def arm(self) -> None:
        """Resets timers and regex targets before a board boot."""
        logger.debug("[Profiler] Armed and waiting for boot milestones...")
        self.targets = {k: re.compile(v) for k, v in self._normalized_names.items()}
        self.metrics = {k: -1.0 for k in self.targets.keys()}
        self._buffer = ""  # <--- Clear the rolling buffer
        self._t_zero = time.perf_counter()

    def process_chunk(self, chunk: str) -> None:
        """Evaluates raw byte chunks instantly to catch prompts lacking newlines."""
        if not self.targets:
            return

        self._buffer += chunk

        # ==========================================
        # THE ANSI ESCAPE CODE SHIELD
        # ==========================================
        # Strips terminal colors and cursor reports so regexes match clean text
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean_buffer = ansi_escape.sub('', self._buffer)

        current_time = round(time.perf_counter() - self._t_zero, 3)
        hit_keys = []

        for name, regex in self.targets.items():
            # Evaluate against the CLEAN buffer
            if regex.search(clean_buffer):
                self.metrics[name] = current_time
                hit_keys.append(name)
                # Replaced sys.stdout hack with proper Enterprise logging
                # so the milestones are captured in the Pytest XML/JSON artifacts.
                logger.info(f"[Profiler] Milestone '{name}' reached at {current_time}s")

        for k in hit_keys:
            del self.targets[k]

        # Prevent the string from growing infinitely in RAM
        if len(self._buffer) > 8192:
            self._buffer = self._buffer[-8192:]

    def fetch_metrics(self) -> Dict[str, float]:
        missed = [k for k, v in self.metrics.items() if v == -1.0]
        if missed:
            logger.warning(f"[Profiler] Missed boot milestones: {missed}")
        return self.metrics


# ==========================================
# PHYSICAL TRANSPORT CONTRACT
# ==========================================
class EphemeralSerialClient:
    """
    Bare-metal UART transport layer with a Live Terminal Architecture.
    Streams console output to stdout while simultaneously routing data
    to execution blocks and background profilers.
    """
    def __init__(self, cfg: HostSerialConfig, expected_prompt: str = "# "):
        self.cfg = cfg
        self.prompt = expected_prompt
        self.conn: Optional[serial.Serial] = None

        # Threading & Routing State
        self._stop_event = threading.Event()
        self._rx_thread: Optional[threading.Thread] = None
        self._profiler: Optional[LiveBootProfiler] = None

        # Synchronous Execution State
        self._cmd_lock = threading.Lock()
        self._cmd_output = ""
        self._waiting_for_prompt = False
        self._prompt_event = threading.Event()

    # ------------------------------------------
    # LIFECYCLE MANAGEMENT
    # ------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self.conn is not None and self.conn.is_open

    def attach_profiler(self, profiler: LiveBootProfiler) -> None:
        """Injects a state machine to evaluate the live RX stream."""
        self._profiler = profiler
        self._profiler.arm()

    def connect(self) -> None:
        if self.is_connected:
            return

        logger.debug(f"[Serial] Binding physical UART on {self.cfg.port} at {self.cfg.baudrate} baud...")
        try:
            self.conn = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=0.1,  # Short timeout keeps the background thread responsive
                exclusive=True
            )
            self.conn.reset_input_buffer()
            self.conn.reset_output_buffer()

            # Spin up the background terminal emulator
            self._stop_event.clear()
            self._rx_thread = threading.Thread(target=self._rx_daemon, daemon=True)
            self._rx_thread.start()

            # Synchronize with the embedded shell
            self._sync_prompt()
            logger.info(f"[Serial] Transport established and synchronized on {self.cfg.port}.")

        except serial.SerialException as e:
            err_msg = f"Failed to open UART {self.cfg.port}: {e}"
            logger.critical(f"[Serial] FATAL: {err_msg}")
            raise TransportConnectionError(err_msg)

    def disconnect(self) -> None:
        """Zero-Leakage Teardown."""
        logger.debug(f"[Serial] ZERO-LEAKAGE: Tearing down UART transport on {self.cfg.port}.")
        self._stop_event.set()
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=2.0)

        if self.conn and self.conn.is_open:
            self.conn.close()

    # ------------------------------------------
    # THE TERMINAL EMULATOR CORE
    # ------------------------------------------
    def _rx_daemon(self) -> None:
        """
        Continuously reads the hardware buffer.
        Routes completely formed lines into the Pytest DEBUG logger (-vv).
        """
        line_buffer = ""

        while not self._stop_event.is_set() and self.conn and self.conn.is_open:
            try:
                raw_data = self.conn.read(max(1, self.conn.in_waiting))
                if not raw_data:
                    continue

                decoded = raw_data.decode('utf-8', errors='replace')
                line_buffer += decoded

                # ==========================================
                # THE MATRIX ROUTER (-vv)
                # ==========================================
                while '\n' in line_buffer:
                    line, line_buffer = line_buffer.split('\n', 1)
                    clean_line = line.strip('\r')

                    # This pipes the UART stream directly into Pytest's logging engine.
                    # It will only appear on the screen if the operator ran `pytest -vv`
                    if clean_line:
                        logger.debug(f"[UART] RX <- {clean_line}")

                    # Route to Profiler
                    if self._profiler:
                        self._profiler.process_chunk(clean_line + '\n')

                # Synchronous routing for safe_run()
                with self._cmd_lock:
                    if self._waiting_for_prompt:
                        self._cmd_output += decoded
                        if self._cmd_output.endswith(self.prompt):
                            self._prompt_event.set()

            except serial.SerialException as e:
                logger.critical(f"[Serial] FATAL: Hardware disconnected in background thread! {e}")
                self._stop_event.set()
                break

    def _sync_prompt(self, timeout_s: float = 3.0) -> None:
        """Sends a newline and blocks until the RX Daemon sees the prompt."""
        with self._cmd_lock:
            self._cmd_output = ""
            self._waiting_for_prompt = True
            self._prompt_event.clear()

        logger.debug("[Serial] TX -> [Enter] (Syncing Shell...)")
        self.conn.write(b"\r\n")
        self.conn.flush()

        if not self._prompt_event.wait(timeout_s):
            with self._cmd_lock:
                self._waiting_for_prompt = False

            err_msg = "Failed to sync to shell prompt on connect. Board dead or kernel panic?"
            logger.critical(f"[Serial] FATAL: {err_msg}")
            raise TransportConnectionError(err_msg)

        with self._cmd_lock:
            self._waiting_for_prompt = False

    # ------------------------------------------
    # COMMAND EXECUTION
    # ------------------------------------------
    def safe_run(self, cmd: str, timeout_s: float = 30.0, check_exit_code: bool = True, **kwargs: Any) -> CommandResult:
        if not self.is_connected or not self.conn:
            raise TransportConnectionError("Serial port is closed.")

        t0 = time.perf_counter()

        with self._cmd_lock:
            self._cmd_output = ""
            self._waiting_for_prompt = True
            self._prompt_event.clear()

        try:
            # 1. Transmit
            logger.debug(f"[UART] TX -> {cmd}")
            self.conn.write(f"{cmd}\n".encode('utf-8'))
            self.conn.flush()

            # 2. Await OS execution
            if not self._prompt_event.wait(timeout_s):
                with self._cmd_lock:
                    self._waiting_for_prompt = False

                err_msg = f"Command '{cmd}' timed out after {timeout_s}s."
                logger.critical(f"[UART] FATAL: {err_msg}")
                raise TransportTimeoutError(err_msg)

            with self._cmd_lock:
                raw_stdout = self._cmd_output
                self._waiting_for_prompt = False

            cleaned_stdout = self._clean_output(raw_stdout, cmd)

            # 3. Retrieve Exit Code
            exit_code = 0
            if check_exit_code:
                with self._cmd_lock:
                    self._cmd_output = ""
                    self._waiting_for_prompt = True
                    self._prompt_event.clear()

                logger.debug(f"[UART] TX -> echo $?")
                self.conn.write(b"echo $?\n")
                self.conn.flush()

                if self._prompt_event.wait(2.0):
                    with self._cmd_lock:
                        rc_raw = self._cmd_output
                    rc_clean = self._clean_output(rc_raw, "echo $?").strip()
                    match = re.search(r'\d+', rc_clean)
                    exit_code = int(match.group()) if match else -1

                with self._cmd_lock:
                    self._waiting_for_prompt = False

        except serial.SerialException as e:
            self.disconnect()
            err_msg = f"UART pipe shattered during execution: {e}"
            logger.critical(f"[UART] FATAL: {err_msg}")
            raise TransportConnectionError(err_msg)

        duration = round(time.perf_counter() - t0, 3)
        is_ok = (exit_code == 0)

        logger.debug(f"[UART] Command executed in {duration}s (Exit Code: {exit_code}).")

        return CommandResult(
            command=cmd,
            stdout=cleaned_stdout if is_ok else "",
            stderr=cleaned_stdout if not is_ok else "",
            exited=exit_code,
            ok=is_ok,
            duration_s=duration
        )

    def _clean_output(self, raw_text: str, sent_cmd: str) -> str:
        """Strips the echoed command and the trailing prompt."""
        lines = raw_text.replace('\r\n', '\n').split('\n')
        if lines and sent_cmd in lines[0]:
            lines = lines[1:]
        cleaned = '\n'.join(lines).replace(self.prompt, '').strip()
        return cleaned
