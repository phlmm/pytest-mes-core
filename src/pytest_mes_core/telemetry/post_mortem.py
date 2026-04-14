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
        logger.debug(f"[Post-Mortem] RPC TX -> {cmd}")

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
                    logger.critical(f"[Post-Mortem] FATAL: OpenOCD RPC command '{cmd}' timed out!")
                    break

            # Strip the final prompt from the output for clean logging
            clean_output = output.rsplit("\n> ", 1)[0].strip()
            # Truncate debug output if it's a massive memory dump
            logger.debug(f"[Post-Mortem] RPC RX <- {clean_output[:200]}{'...' if len(clean_output) > 200 else ''}")
            return clean_output

    def execute_hardware_dump(
        self,
        dcc_addr: Optional[str] = None,
        stack_addr: Optional[str] = None
    ) -> Dict[str, str]:
        """Extracts raw silicon state based strictly on configured addresses."""
        logger.critical("="*60)
        logger.critical(f"[Post-Mortem] FATAL: TEST FAILURE DETECTED. INITIATING HARDWARE CRASH DUMP!")
        logger.critical(f"[Post-Mortem] Freezing crime scene via OpenOCD RPC (Port {self.rpc_port})...")
        logger.critical("="*60)

        crash_data = {}
        try:
            # 1. Halt the CPU immediately to freeze the crime scene
            self._send_rpc("halt")

            # 2. Dump all CPU registers
            logger.debug("[Post-Mortem] Extracting CPU registers...")
            crash_data['registers'] = self._send_rpc("reg")

            # 3. Conditionally dump the ARM DCC Console
            if dcc_addr:
                logger.debug(f"[Post-Mortem] Extracting ARM DCC Console from {dcc_addr}...")
                crash_data['dcc_console'] = self._send_rpc(f"read_memory {dcc_addr} 32 100")

            # 4. Conditionally dump the raw Call Stack
            if stack_addr:
                logger.debug(f"[Post-Mortem] Extracting raw stack memory from {stack_addr}...")
                crash_data['raw_stack'] = self._send_rpc(f"mdw {stack_addr} 64")

        except Exception as e:
            logger.critical(f"[Post-Mortem] FATAL: Hardware dump failed. JTAG/SWD connection dropped? {e}")
            crash_data['error'] = str(e)

        return crash_data

    def execute_gdb_backtrace(self, elf_path: Path) -> str:
        """
        Uses headless GDB to generate a human-readable C-code backtrace.
        Uses thread-safe temporary files to prevent parallel worker collisions.
        """
        if not elf_path.exists():
            logger.warning(f"[Post-Mortem] No ELF file found at {elf_path}. Skipping GDB backtrace.")
            return f"No ELF file found at {elf_path}."

        logger.info(f"[Post-Mortem] Executing Headless GDB Backtrace on port {self.gdb_port}...")

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
                logger.debug(f"[Post-Mortem] Spawning local Host PC process: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15.0)

                if result.returncode != 0:
                    logger.error(f"[Post-Mortem] GDB unwinding failed (Code {result.returncode}): {result.stderr.strip()}")
                    return f"GDB Error (Code {result.returncode}): {result.stderr}"

                logger.info("[Post-Mortem] GDB Backtrace successfully generated.")
                return result.stdout

            except subprocess.TimeoutExpired:
                err_msg = "GDB Unwind timed out. Target CPU might be entirely deadlocked or JTAG clock failed."
                logger.critical(f"[Post-Mortem] FATAL: {err_msg}")
                return err_msg
            except FileNotFoundError:
                err_msg = f"GDB toolchain '{self.gdb_toolchain}' not found in system PATH."
                logger.critical(f"[Post-Mortem] FATAL: {err_msg}")
                return err_msg
