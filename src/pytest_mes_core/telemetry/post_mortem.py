# src/pytest_mes_core/telemetry/post_mortem.py
import time
import socket
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("mes_core.telemetry.post_mortem")

class JtagCrashDumper:
    """
    Connects to an active OpenOCD daemon upon test failure.
    Halts the CPU, dumps registers, extracts the ARM DCC log,
    and saves everything to the Telemetry sink for R&D.
    """
    def __init__(self, rpc_port: int, gdb_port: int = 3333):
        self.rpc_port = rpc_port
        self.gdb_port = gdb_port

    def _send_rpc(self, cmd: str) -> str:
        """Helper to send a command to OpenOCD and read the response."""
        with socket.create_connection(('127.0.0.1', self.rpc_port), timeout=5) as s:
            s.recv(1024) # Eat the banner
            s.sendall(f"{cmd}\n".encode('utf-8'))
            time.sleep(0.2)
            return s.recv(8192).decode('utf-8').replace('>', '').strip()

    def execute_hardware_dump(self) -> dict:
        """Extracts raw silicon state without needing an ELF file."""
        logger.warning("[Post-Mortem] Halting CPU to extract raw JTAG state...")

        crash_data = {}
        try:
            # 1. Halt the CPU immediately to freeze the crime scene
            self._send_rpc("halt")

            # 2. Dump all CPU registers (PC, SP, LR, R0-R15)
            crash_data['registers'] = self._send_rpc("reg")

            # 3. Dump the ARM DCC (Debug Communications Channel)
            # This requires OpenOCD to have been configured with `target request debugmsgs enable`
            crash_data['dcc_console'] = self._send_rpc("read_memory 0x20000000 32 100") # Adjust address to your DCC buffer

            # 4. Dump the raw Call Stack (e.g., top 256 bytes of RAM where the Stack Pointer is)
            # You would parse the SP from the 'reg' command, but for example:
            crash_data['raw_stack'] = self._send_rpc("mdw 0x2001FF00 64")

        except Exception as e:
            logger.error(f"[Post-Mortem] Hardware dump failed: {e}")
            crash_data['error'] = str(e)

        return crash_data

    def execute_gdb_backtrace(self, elf_path: Path) -> str:
        """
        Uses headless GDB to generate a human-readable C-code backtrace.
        Requires arm-none-eabi-gdb to be installed on the Host PC.
        """
        if not elf_path.exists():
            return "No ELF file provided for backtrace."

        logger.warning("[Post-Mortem] Executing Headless GDB Backtrace...")

        # We write a temporary GDB script to automate the connection and unwinding
        gdb_script = f"""
        target extended-remote localhost:{self.gdb_port}
        bt full
        info locals
        info threads
        detach
        quit
        """

        script_path = Path("/tmp/mes_crash.gdb")
        script_path.write_text(gdb_script)

        cmd = [
            "arm-none-eabi-gdb",
            "--batch",
            "--command=/tmp/mes_crash.gdb",
            str(elf_path)
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return result.stdout
        except Exception as e:
            return f"GDB Unwind failed: {e}"
