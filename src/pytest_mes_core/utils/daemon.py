import anyio
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

    def __init__(self, cmd: List[str], logger: logging.Logger, ready_phrase: Optional[str]=None):
        self.cmd = cmd
        self.cmd_str = ' '.join(cmd)
        self.logger = logger
        self.ready_phrase = ready_phrase.lower() if ready_phrase else None
        self.proc: Optional[subprocess.Popen] = None
        self.stdout_log: List[str] = []
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()

    def start(self, timeout_s: float=5.0) -> 'DaemonProcess':
        """Spawns the daemon and blocks until the ready_phrase is detected.

        Args:
            timeout_s: The maximum number of seconds to wait for the ready phrase.

        Returns:
            DaemonProcess: The current instance, for chaining.

        Raises:
            DaemonStartupError: If the daemon crashes instantly or times out waiting.
        """
        self.logger.debug(f'[Daemon] Spawning background process: {self.cmd_str}')
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self._thread = threading.Thread(target=self._io_consumer, daemon=True)
        self._thread.start()
        if self.ready_phrase:
            self.logger.debug(f"[Daemon] Waiting up to {timeout_s}s for ready phrase: '{self.ready_phrase}'...")
            is_ready = self._ready_event.wait(timeout=timeout_s)
            if not is_ready:
                self.stop()
                crash_log = ''.join(self.stdout_log[-10:])
                err_msg = f'Daemon timed out waiting for ready state. Output:\n{crash_log}'
                self.logger.critical(f'[Daemon] FATAL: {err_msg}')
                raise DaemonStartupError(err_msg)
        else:
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                self.stop()
                raise DaemonStartupError('Daemon startup interrupted by operator (Ctrl+C).')
            if self.proc.poll() is not None:
                retcode = self.proc.returncode
                self.stop()
                raise DaemonStartupError(f'Daemon crashed instantly. Code: {retcode}')
        self.logger.info(f'[Daemon] Process online and backgrounded (PID: {self.proc.pid}).')
        return self

    async def async_start(self, timeout_s: float = 5.0) -> 'DaemonProcess':
        return await anyio.to_thread.run_sync(self.start, timeout_s)

    def stop(self) -> None:
        """ZERO-LEAKAGE: Terminates the daemon and reaps the worker thread."""
        self._stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.logger.debug(f'[Daemon] ZERO-LEAKAGE: Terminating PID {self.proc.pid}...')
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.logger.warning(f'[Daemon] Process refused SIGTERM. Executing SIGKILL.')
                self.proc.kill()
                self.proc.wait()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.proc = None

    async def async_stop(self) -> None:
        return await anyio.to_thread.run_sync(self.stop)

    def _io_consumer(self) -> None:
        """Background thread that consumes the subprocess pipe."""
        if not self.proc or not self.proc.stdout:
            return
        for line in iter(self.proc.stdout.readline, ''):
            if self._stop_event.is_set():
                break
            clean_line = line.strip()
            if clean_line:
                self.stdout_log.append(clean_line + '\n')
                if self.ready_phrase and (not self._ready_event.is_set()):
                    if self.ready_phrase in clean_line.lower():
                        self._ready_event.set()

    def export_log(self, export_dir: Path) -> Path:
        """Dumps the daemon's lifetime output to a discrete file.

        Args:
            export_dir: Directory where the log file should be saved.

        Returns:
            Path: The full path to the exported log file.
        """
        export_dir.mkdir(parents=True, exist_ok=True)
        filename = f'{Path(self.cmd[0]).name}_daemon_{int(time.time())}.log'
        filepath = export_dir / filename
        with open(filepath, 'w') as f:
            f.write(f'DAEMON COMMAND: {self.cmd_str}\n')
            f.write('-' * 40 + '\n')
            f.writelines(self.stdout_log)
        return filepath
    async def async_export_log(self, export_dir: Path) -> Path:
        return await anyio.to_thread.run_sync(self.export_log, export_dir)
