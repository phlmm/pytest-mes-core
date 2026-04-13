# src/pytest_mes_core/telemetry/post_mortem.py
import time
import socket
import logging
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("mes_core.telemetry.post_mortem")

class JtagCrashDumper:
    """
    Connects to an active OpenOCD daemon upon test failure.
    Safely orchestrates hardware state extraction and Headless GDB unwinding
    without relying on hardcoded silicon magic numbers.
    """
    def __init__(
        self,
        rpc_port: int,
        gdb_port: int = 3333,
        gdb_toolchain: str = "gdb-multiarch"
    ):
        self.rpc_port = rpc_port
        self.gdb_port = gdb_port
        self.gdb_toolchain = gdb_toolchain

    def _send_rpc(self, cmd: str, timeout_s: float = 5.0) -> str:
        """Robust, EMI-resistant OpenOCD RPC client."""
        with socket.create_connection(('127.0.0.1', self.rpc_port), timeout=timeout_s) as s:
            s.recv(1024) # Eat the telnet banner
            s.sendall(f"{cmd}\n".encode('utf-8'))

            output = ""
            while True:
                try:
                    # errors='replace' prevents UnicodeDecodeError from JTAG EMI noise
                    chunk = s.recv(4096).decode('utf-8', errors='replace')
                    if not chunk:
                        break
                    output += chunk

                    # Strictly wait for the OpenOCD ready prompt
                    if output.endswith("\n> ") or output.endswith("\r\n> "):
                        break
                except socket.timeout:
                    logger.warning(f"[Post-Mortem] RPC command '{cmd}' timed out.")
                    break

            # Strip the final prompt from the output for clean logging
            return output.rsplit("\n> ", 1)[0].strip()

    def execute_hardware_dump(
        self,
        dcc_addr: Optional[str] = None,
        stack_addr: Optional[str] = None
    ) -> Dict[str, str]:
        """Extracts raw silicon state based strictly on configured addresses."""
        logger.warning(f"[Post-Mortem] Halting CPU via RPC port {self.rpc_port} to extract state...")

        crash_data = {}
        try:
            # 1. Halt the CPU immediately to freeze the crime scene
            self._send_rpc("halt")

            # 2. Dump all CPU registers
            crash_data['registers'] = self._send_rpc("reg")

            # 3. Conditionally dump the ARM DCC Console
            if dcc_addr:
                crash_data['dcc_console'] = self._send_rpc(f"read_memory {dcc_addr} 32 100")

            # 4. Conditionally dump the raw Call Stack
            if stack_addr:
                crash_data['raw_stack'] = self._send_rpc(f"mdw {stack_addr} 64")

        except Exception as e:
            logger.error(f"[Post-Mortem] Hardware dump failed: {e}")
            crash_data['error'] = str(e)

        return crash_data

    def execute_gdb_backtrace(self, elf_path: Path) -> str:
        """
        Uses headless GDB to generate a human-readable C-code backtrace.
        Uses thread-safe temporary files to prevent parallel worker collisions.
        """
        if not elf_path.exists():
            return f"No ELF file found at {elf_path}."

        logger.warning(f"[Post-Mortem] Executing Headless GDB Backtrace on port {self.gdb_port}...")

        # Using pwndbg/GEF compatible commands to extract maximum context
        gdb_commands = f"""
        target extended-remote localhost:{self.gdb_port}
        set pagination off
        echo \\n=== THREADS ===\\n
        info threads
        echo \\n=== BACKTRACE ===\\n
        bt full
        echo \\n=== LOCALS ===\\n
        info locals
        detach
        quit
        """

        # Thread-Safe Temp File (Auto-deletes when the 'with' block exits)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".gdb", delete=True) as temp_script:
            temp_script.write(gdb_commands)
            temp_script.flush()

            cmd = [
                self.gdb_toolchain,
                "--batch",
                f"--command={temp_script.name}",
                str(elf_path.resolve())
            ]

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15.0)
                if result.returncode != 0:
                    return f"GDB Error (Code {result.returncode}): {result.stderr}"
                return result.stdout
            except subprocess.TimeoutExpired:
                return "GDB Unwind timed out. Target CPU might be deadlocked."
            except FileNotFoundError:
                return f"GDB toolchain '{self.gdb_toolchain}' not found in system PATH."
