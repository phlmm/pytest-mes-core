import os
import subprocess
import time
import logging
import socket
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mes_core.host_adapters.renode")

class RenodeRunnerError(Exception):
    pass

class RenodeRunner:
    """
    Manages the lifecycle of a headless Renode simulation process.
    
    This acts as a digital twin for the physical hardware, allowing the
    pytest-mes-core FSM to execute its boot sequences and integration tests
    without a physical board attached.
    """
    
    def __init__(self, script_path: Path, monitor_port: int = 3333, uart_port: int = 1234):
        self.script_path = script_path
        self.monitor_port = monitor_port
        self.uart_port = uart_port
        self._process: Optional[subprocess.Popen] = None
        
    def start(self) -> None:
        """Spawns the headless Renode process and waits for the monitor port."""
        if not self.script_path.exists():
            raise FileNotFoundError(f"Renode script not found: {self.script_path}")
            
        logger.info(f"[Renode] Starting headless simulation using: {self.script_path.name}")
        
        # We start Renode in headless mode, tell it to load the script, 
        # and start the monitor on a specific TCP port so our Virtual PSU can control it.
        cmd = [
            "renode",
            "--disable-x11",
            "--port", str(self.monitor_port),
            "-e", f"s @{self.script_path.absolute()}"
        ]
        
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Wait for the monitor port to open
        if not self._wait_for_port(self.monitor_port, timeout=10.0):
            self.stop()
            raise RenodeRunnerError(f"Renode failed to bind monitor port {self.monitor_port}")
            
        logger.info(f"[Renode] Simulation running. Monitor: {self.monitor_port}, UART: {self.uart_port}")
        
    def stop(self) -> None:
        """Gracefully terminates the Renode process."""
        if self._process:
            logger.info("[Renode] Terminating simulation process...")
            self._process.terminate()
            try:
                self._process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def _wait_for_port(self, port: int, timeout: float) -> bool:
        """Polls a TCP port until it accepts connections."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    return True
            time.sleep(0.5)
        return False

    def __enter__(self):
        self.start()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
