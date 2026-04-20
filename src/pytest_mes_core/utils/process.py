# src/pytest_mes_core/utils/process.py
import sys
import time
import json
import logging
import subprocess
from pathlib import Path
from typing import List, Optional

class ProcessExecutionError(Exception):
    pass

class ProcessTimeoutError(ProcessExecutionError):
    pass

class LiveProcess:
    """
    Stateful object representing an OS-level subprocess.
    Handles live terminal streaming, telemetry capture, and artifact exporting.
    """
    def __init__(self, cmd: List[str], timeout_s: float, logger: logging.Logger):
        self.cmd = cmd
        self.cmd_str = " ".join(cmd)
        self.timeout_s = timeout_s
        self.logger = logger

        # Telemetry State
        self.stdout: str = ""
        self.returncode: Optional[int] = None
        self.duration_s: float = 0.0
        self.executed: bool = False

    def execute(self) -> 'LiveProcess':
        """Runs the process, streams to console, and captures telemetry.

        Returns:
            LiveProcess: The current instance containing telemetry and status.

        Raises:
            RuntimeError: If the process has already been executed.
            ProcessTimeoutError: If the command times out.
            ProcessExecutionError: If the process execution fails unexpectedly.
        """
        if self.executed:
            raise RuntimeError(f"Process '{self.cmd[0]}' has already been executed.")

        self.logger.debug(f"[OS] Executing: {self.cmd_str}")
        t0 = time.perf_counter()

        proc = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # Merge stderr into stdout
            text=True,
            bufsize=1
        )

        _stdout_chunks: list = []

        try:
            while True:
                # 16-byte chunks defend against RAM spikes and support '\r' progress bars
                chunk = proc.stdout.read(16)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    # Yield to the OS when the subprocess is alive but silent.
                    # Without this the loop busy-spins at 100% CPU, starving UART
                    # reader threads in multi-jig environments.
                    time.sleep(0.005)
                    continue
                sys.stdout.write(chunk)
                sys.stdout.flush()
                _stdout_chunks.append(chunk)

            proc.wait(timeout=self.timeout_s)
            self.returncode = proc.returncode
            self.stdout = "".join(_stdout_chunks)

        except subprocess.TimeoutExpired:
            self.logger.critical(f"\n[OS] FATAL: Process hung for >{self.timeout_s}s! Executing hard kill.")
            proc.kill()
            proc.wait()
            self.returncode = -1
            raise ProcessTimeoutError(f"Command timed out: {self.cmd_str}")
        except Exception as e:
            self.logger.critical(f"\n[OS] FATAL: Subprocess failure: {e}")
            self.returncode = -2
            raise ProcessExecutionError(f"Process execution failed: {e}")
        finally:
            # ZERO-LEAKAGE: Reap the zombie if Python threads panic
            if proc.poll() is None:
                proc.kill()
                proc.wait()

            self.duration_s = round(time.perf_counter() - t0, 3)
            self.executed = True

        return self

    # ==========================================
    # THE EXPORTERS
    # ==========================================
    def export_log(self, export_dir: Path) -> Path:
        """Dumps the raw unedited output to a discrete text file for CI/CD artifacts.

        Args:
            export_dir: Directory where the log file should be saved.

        Returns:
            Path: The full path to the exported log file.

        Raises:
            RuntimeError: If called before the process has been executed.
        """
        if not self.executed:
            raise RuntimeError("Cannot export a process that hasn't run.")

        export_dir.mkdir(parents=True, exist_ok=True)
        # E.g., bmaptool_1710432000.log
        filename = f"{Path(self.cmd[0]).name}_{int(time.time())}.log"
        filepath = export_dir / filename

        with open(filepath, "w") as f:
            f.write(f"COMMAND: {self.cmd_str}\n")
            f.write(f"EXIT CODE: {self.returncode}\n")
            f.write(f"DURATION: {self.duration_s}s\n")
            f.write("-" * 40 + "\n")
            f.write(self.stdout)

        self.logger.debug(f"[OS] Process trace exported to {filepath}")
        return filepath

    def to_dict(self) -> dict:
        """Serializes the telemetry for injection into Pytest JSON reports.

        Returns:
            dict: Telemetry data containing cmd, returncode, duration, and output size.
        """
        return {
            "cmd": self.cmd_str,
            "returncode": self.returncode,
            "duration_s": self.duration_s,
            "output_length_bytes": len(self.stdout)
        }
