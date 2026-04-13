# src/pytest_mes_core/transports/chunking.py
import threading
import logging
from typing import List, Optional

from pytest_mes_core.transports.base import DutTransport, TransportConnectionError

logger = logging.getLogger("mes_core.transports.chunking")

class HostSideBuffer:
    """
    Asynchronous Data Vacuum.
    Continuously tails a remote log file across ANY transport and buffers it in Host PC RAM.
    Guarantees data survival even if the DUT kernel panics and the socket drops.
    """
    def __init__(self, transport: DutTransport, remote_path: str, poll_interval_s: float = 1.0):
        self.transport = transport
        self.remote_path = remote_path
        self.poll_interval_s = poll_interval_s

        # Shared State (Must be protected by Lock)
        self._buffer: List[str] = []
        self._lines_read = 0
        self._lock = threading.Lock()

        # Thread Control
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Spawns the background daemon to begin data extraction."""
        logger.debug(f"[HostBuffer] Armed asynchronous vacuum for {self.remote_path}...")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> List[str]:
        """Halts the polling instantly and returns a thread-safe copy of the surviving data."""
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            # Because we use Event.wait() in the loop, it should exit almost instantly.
            self._thread.join(timeout=self.poll_interval_s + 0.5)
            if self._thread.is_alive():
                logger.warning(f"[HostBuffer] Thread join timed out. Transport socket may be severely hung.")

        # Safely copy the payload before returning so the test logic doesn't mutate internal state
        with self._lock:
            return list(self._buffer)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    start_line = self._lines_read + 1

                cmd = f"tail -n +{start_line} {self.remote_path} 2>/dev/null"

                # 1. Transport Agnostic Execution
                # We use a strict timeout to avoid deadlocking the background thread.
                # We floor it at 2.0s so slow Serial connections aren't falsely flagged as dead.
                run_timeout = max(2.0, self.poll_interval_s * 1.5)
                res = self.transport.safe_run(cmd, timeout_s=run_timeout)

                if res.ok and res.stdout:
                    new_lines = [l for l in res.stdout.strip().split('\n') if l.strip()]

                    if new_lines:
                        with self._lock:
                            self._buffer.extend(new_lines)
                            self._lines_read += len(new_lines)

            except TransportConnectionError:
                # Explicit Domain Exception caught (e.g., SSH Pipe Shattered)
                with self._lock:
                    survived = len(self._buffer)
                logger.warning(f"[HostBuffer] Transport severed (DUT Crash/Panic). Data survived: {survived} lines.")
                break

            except Exception as e:
                # Generic fallback for unexpected transport or thread faults
                with self._lock:
                    survived = len(self._buffer)
                logger.debug(f"[HostBuffer] Vacuum interrupted: {e}. Data survived: {survived} lines.")
                break

            # 2. Responsive Sleep (The Teardown Latency Fix)
            # Instead of time.sleep(), we wait on the stop event.
            # If stop() is called, this wakes up IMMEDIATELY and exits the loop.
            self._stop_event.wait(timeout=self.poll_interval_s)
