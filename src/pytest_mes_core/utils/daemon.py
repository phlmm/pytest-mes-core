# src/pytest_mes_core/utils/daemon.py
import time
import queue
import logging
import threading
import subprocess
from pathlib import Path
from typing import List, Optional

class DaemonStartupError(Exception):
    pass

class DaemonProcess:
    """
    Stateful manager for asynchronous, long-running background processes.
    Features non-blocking IO consumption, regex ready-state polling, and Zero-Leakage teardown.
    """
    def __init__(self, cmd: List[str], logger: logging.Logger, ready_phrase: Optional[str] = None):
        self.cmd = cmd
        self.cmd_str = " ".join(cmd)
        self.logger = logger
        self.ready_phrase = ready_phrase.lower() if ready_phrase else None

        self.proc: Optional[subprocess.Popen] = None
        self.stdout_log: List[str] = []
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()

    def start(self, timeout_s: float = 5.0) -> 'DaemonProcess':
        """Spawns the daemon and blocks until the ready_phrase is detected."""
        self.logger.debug(f"[Daemon] Spawning background process: {self.cmd_str}")

        self.proc = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # Merge stderr for complete logs
            text=True,
            bufsize=1 # Line buffered
        )

        # Spawn the IO consumer thread to prevent OS pipe deadlocks
        self._thread = threading.Thread(target=self._io_consumer, daemon=True)
        self._thread.start()

        # Wait for the process to broadcast its ready state
        if self.ready_phrase:
            self.logger.debug(f"[Daemon] Waiting up to {timeout_s}s for ready phrase: '{self.ready_phrase}'...")
            is_ready = self._ready_event.wait(timeout=timeout_s)

            if not is_ready:
                self.stop()
                crash_log = "".join(self.stdout_log[-10:]) # Grab last 10 lines
                err_msg = f"Daemon timed out waiting for ready state. Output:\n{crash_log}"
                self.logger.critical(f"[Daemon] FATAL: {err_msg}")
                raise DaemonStartupError(err_msg)
        else:
            # If no ready phrase is provided, just give the OS 0.5s to fail-fast
            time.sleep(0.5)
            if self.proc.poll() is not None:
                self.stop()
                raise DaemonStartupError(f"Daemon crashed instantly. Code: {self.proc.returncode}")

        self.logger.info(f"[Daemon] Process online and backgrounded (PID: {self.proc.pid}).")
        return self

    def stop(self) -> None:
        """ZERO-LEAKAGE: Terminates the daemon and reaps the worker thread."""
        self._stop_event.set()

        if self.proc and self.proc.poll() is None:
            self.logger.debug(f"[Daemon] ZERO-LEAKAGE: Terminating PID {self.proc.pid}...")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.logger.warning(f"[Daemon] Process refused SIGTERM. Executing SIGKILL.")
                self.proc.kill()
                self.proc.wait()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

        self.proc = None

    def _io_consumer(self) -> None:
        """Background thread that consumes the subprocess pipe."""
        if not self.proc or not self.proc.stdout:
            return

        # iter() blockingly reads line-by-line until EOF
        for line in iter(self.proc.stdout.readline, ''):
            if self._stop_event.is_set():
                break

            clean_line = line.strip()
            if clean_line:
                self.stdout_log.append(clean_line + "\n")

                # Matrix Tracing: Un-comment this if you want daemon output in -vv
                # self.logger.debug(f"[Daemon-RX] {clean_line}")

                # Check for the ready phrase
                if self.ready_phrase and not self._ready_event.is_set():
                    if self.ready_phrase in clean_line.lower():
                        self._ready_event.set()

    def export_log(self, export_dir: Path) -> Path:
        """Dumps the daemon's lifetime output to a discrete file."""
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{Path(self.cmd[0]).name}_daemon_{int(time.time())}.log"
        filepath = export_dir / filename

        with open(filepath, "w") as f:
            f.write(f"DAEMON COMMAND: {self.cmd_str}\n")
            f.write("-" * 40 + "\n")
            f.writelines(self.stdout_log)

        return filepath
