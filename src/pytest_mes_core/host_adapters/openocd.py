# src/pytest_mes_core/host_adapters/openocd.py
import time
import socket
import logging
import subprocess
from typing import Optional, Any

from pytest_mes_core.host_adapters.base import (
    BaseHostAdapter,
    HostAdapterError,
    HostHardwareDisconnectError
)

logger = logging.getLogger("mes_core.host_adapters.openocd")

class HostOpenOcdError(HostAdapterError):
    pass

class OpenOcdDaemonAdapter(BaseHostAdapter):
    """
    Manages the lifecycle of the OpenOCD daemon to guarantee Zero-Leakage
    of physical USB JTAG/SWD debug probes.
    """
    def __init__(self, interface_cfg: str, target_cfg: str, rpc_port: int = 4444):
        self.interface_cfg = interface_cfg
        self.target_cfg = target_cfg
        self.rpc_port = rpc_port
        self._process: Optional[subprocess.Popen] = None

    def __enter__(self) -> 'OpenOcdDaemonAdapter':
        logger.info(f"[JTAG] Spinning up OpenOCD Daemon on port {self.rpc_port}...")

        cmd = [
            "openocd",
            "-f", self.interface_cfg,
            "-f", self.target_cfg,
            "-c", f"telnet_port {self.rpc_port}",
            "-c", "gdb_port disabled",
            "-c", "tcl_port disabled"
        ]

        try:
            # Spawn in the background
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
        except FileNotFoundError:
            raise HostAdapterError("OpenOCD is not installed on the Host PC.")

        # WAIT FOR DAEMON TO BIND TO USB AND OPEN RPC PORT
        t0 = time.perf_counter()
        while (time.perf_counter() - t0) < 5.0:
            if self._process.poll() is not None:
                # Process crashed instantly (usually means USB probe is unplugged)
                stdout, _ = self._process.communicate()
                logger.error(f"[JTAG] OpenOCD crashed on boot:\n{stdout}")
                raise HostHardwareDisconnectError("Failed to bind JTAG probe. Is it plugged in?")

            # Check if RPC port is alive
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex(('127.0.0.1', self.rpc_port)) == 0:
                    logger.debug("[JTAG] OpenOCD Daemon is online and locked to USB.")
                    return self
            time.sleep(0.2)

        self.__exit__(None, None, None)
        raise HostOpenOcdError("OpenOCD Daemon timed out while starting.")

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Execute Order 66 on the OpenOCD daemon."""
        if self._process and self._process.poll() is None:
            logger.debug("[JTAG] ZERO-LEAKAGE: Releasing JTAG USB Probe.")
            self._process.terminate()
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                logger.warning("[JTAG] OpenOCD refused to terminate. Executing SIGKILL.")
                self._process.kill()
                self._process.wait() # Reap the zombie
            finally:
                self._process = None
