import structlog
import os
import subprocess
import time
import logging
import socket
from pathlib import Path
from typing import Optional
logger = structlog.get_logger('mes_core.host_adapters.renode')

class RenodeRunnerError(Exception):
    pass

class RenodeRunner:
    """
    Manages the lifecycle of a headless Renode simulation process.
    
    This acts as a digital twin for the physical hardware, allowing the
    pytest-mes-core FSM to execute its boot sequences and integration tests
    without a physical board attached.
    """

    def __init__(self, script_path: Path, monitor_port: int=3333, uart_port: int=1234):
        self.script_path = script_path
        self.monitor_port = monitor_port
        self.uart_port = uart_port
        self._process: Optional[subprocess.Popen] = None

    def start(self) -> None:
        """Spawns the headless Renode process and waits for the monitor port.
        
        Raises:
            FileNotFoundError: If the Renode script does not exist.
            RenodeRunnerError: If the simulator fails to bind the TCP monitor port.
        """
        if not self.script_path.exists():
            raise FileNotFoundError(f'Renode script not found: {self.script_path}')
        logger.info('starting_headless_simulation_using_name', name=self.script_path.name)
        cmd = ['renode', '--disable-x11', '--port', str(self.monitor_port), '-e', f's @{self.script_path.absolute()}']
        self._process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not self._wait_for_port(self.monitor_port, timeout=10.0):
            self.stop()
            raise RenodeRunnerError(f'Renode failed to bind monitor port {self.monitor_port}')
        logger.info('simulation_running_monitor_monitor_port_uart_uart_port', monitor_port=self.monitor_port, uart_port=self.uart_port)

    def stop(self) -> None:
        """Gracefully terminates the Renode process."""
        if self._process:
            logger.info('[Renode] Terminating simulation process...')
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