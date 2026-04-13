# src/pytest_mes_core/provisioning/jtag.py
import time
import socket
import logging
from pathlib import Path

from pytest_mes_core.provisioning.base import (
    BaseProvisioner,
    ProvisioningError,
    SiliconLockError,
    ImageVerificationError
)

logger = logging.getLogger("mes_core.provisioning.jtag")

class OpenOcdRpcProvisioner(BaseProvisioner):
    """
    Connects to an active OpenOCD Daemon (managed by HostOpenOcdAdapter) via Telnet RPC.
    Executes raw silicon commands to halt, flash, verify, and reset ARM/RISC-V targets.
    """
    def __init__(self, rpc_port: int = 4444, timeout_s: int = 120):
        self.rpc_port = rpc_port
        self.timeout_s = timeout_s

    def provision(self, image_path: Path) -> None:
        if not image_path.exists():
            raise ProvisioningError(f"Firmware image not found: {image_path}")

        logger.info(f"[JTAG] Pushing {image_path.name} via RPC on port {self.rpc_port}...")

        # OpenOCD requires absolute paths with forward slashes, even on Windows
        safe_path = str(image_path.resolve()).replace('\\', '/')
        commands = [
            "reset halt",
            f"program {safe_path} verify reset",
            "exit"
        ]

        full_output = ""

        try:
            with socket.create_connection(('127.0.0.1', self.rpc_port), timeout=self.timeout_s) as s:
                # Read the initial OpenOCD Telnet banner
                s.recv(1024)

                for cmd in commands:
                    logger.debug(f"[JTAG-RPC] -> {cmd}")
                    s.sendall(f"{cmd}\n".encode('utf-8'))

                    cmd_output = ""
                    while True:
                        try:
                            # EMI Defense: errors='replace' prevents UnicodeDecodeError
                            # if electrical noise corrupts the JTAG console output
                            chunk = s.recv(4096).decode('utf-8', errors='replace')
                            if not chunk:
                                break  # Socket closed by daemon

                            cmd_output += chunk

                            # OpenOCD strictly ends its ready-state with a newline followed by '> '
                            if cmd_output.endswith("\n> ") or cmd_output.endswith("\r\n> "):
                                break

                        except socket.timeout:
                            logger.error(f"[JTAG] RPC Command '{cmd}' timed out. Output so far:\n{cmd_output}")
                            raise ProvisioningError(f"JTAG RPC connection timed out after {self.timeout_s}s. Deadlocked SWD bus?")

                    full_output += cmd_output

                self._evaluate_rpc_response(full_output)

        except ConnectionRefusedError:
            raise ProvisioningError(f"Connection refused on port {self.rpc_port}. Is the OpenOCD daemon running?")

    def _evaluate_rpc_response(self, stdout: str) -> None:
        """Parses the daemon's text stream to map cryptic C-errors to Domain Exceptions."""
        stdout_lower = stdout.lower()

        # 1. Check for Hardware Locks
        if "locked" in stdout_lower or "protection" in stdout_lower:
            logger.error(f"[JTAG] Silicon rejected flash. Fuses blown? Log:\n{stdout}")
            raise SiliconLockError("Target silicon is read/write protected.")

        # 2. Check for Verification Failures
        if "verify failed" in stdout_lower or "mismatch" in stdout_lower:
            logger.critical(f"[JTAG] Flash succeeded but VERIFY FAILED. Bad sector on chip? Log:\n{stdout}")
            raise ImageVerificationError("JTAG readback verification failed.")

        # 3. Check for explicitly reported OpenOCD failures
        if "** programming failed **" in stdout_lower or "error:" in stdout_lower:
            logger.error(f"[JTAG] OpenOCD reported an internal error:\n{stdout}")
            raise ProvisioningError("OpenOCD reported a fatal error during the flash operation.")

        # 4. Require POSITIVE confirmation (Defend against silent hangs/aborts)
        if "wrote" not in stdout_lower and "** programming finished **" not in stdout_lower:
            logger.error(f"[JTAG] Missing positive confirmation. Raw output:\n{stdout}")
            raise ProvisioningError("OpenOCD completed without error, but did not confirm data was written.")

        logger.info("[JTAG] Successfully flashed and verified image.")
