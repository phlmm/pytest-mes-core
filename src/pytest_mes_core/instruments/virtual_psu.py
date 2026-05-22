import structlog
import socket
import logging
import time
from typing import Optional
from functools import partial
import anyio

logger = structlog.get_logger('mes_core.instruments.virtual_psu')

class VirtualRenodePsu:
    """
    A Virtual Power Supply that bridges the pytest-mes-core PSU interface 
    to the Renode Monitor.
    
    When the State Machine requests a physical power cycle, this class translates
    it into `machine Reset`, `start`, and `pause` commands sent to Renode over Telnet.
    This allows the exact same FSM logic to control both real hardware and digital twins.
    """

    def __init__(self, host: str='127.0.0.1', monitor_port: int=3333):
        self.host = host
        self.monitor_port = monitor_port
        self._sock: Optional[socket.socket] = None
        self._is_on = False

    def connect(self) -> None:
        """Connects to the Renode Monitor port via raw TCP socket."""
        logger.debug('connecting_to_renode_monitor_at_host_monitor_port', host=self.host, monitor_port=self.monitor_port)
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((self.host, self.monitor_port))
            self._read_until(b'(machine-0)', timeout=2.0)
            logger.info('[Virtual PSU] Connected to Renode Simulation Monitor.')
        except ConnectionRefusedError:
            logger.critical('fatal_could_not_connect_to_renode_monitor_at_host_monitor_port', host=self.host, monitor_port=self.monitor_port)
            logger.critical('Is the RenodeRunner active?')
            raise

    def _read_until(self, expected: bytes, timeout: float = 2.0) -> bytes:
        """Reads from the socket until expected bytes are found or timeout."""
        if not self._sock:
            return b''
        self._sock.settimeout(timeout)
        data = bytearray()
        try:
            while expected not in data:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
        except socket.timeout:
            pass
        return bytes(data)

    def _send_cmd(self, cmd: str) -> str:
        """Sends a command to the Renode monitor and reads the response."""
        if not self._sock:
            return ''
        logger.debug('tx_cmd', cmd=cmd)
        self._sock.sendall(f'{cmd}\n'.encode('ascii'))
        resp = self._read_until(b'(machine-0)', timeout=2.0).decode('utf-8', errors='ignore')
        logger.debug('rx_val', val=resp.strip())
        return resp

    def set_voltage(self, volts: float) -> None:
        """Mock implementation. Renode doesn't simulate analog voltage rails natively."""
        logger.debug('requested_vout_volts_v_ignored_in_simulation', volts=volts)

    def set_current_limit(self, amps: float) -> None:
        """Mock implementation."""
        pass

    def enable_output(self) -> None:
        """Translates FSM Power-On to Renode `start`."""
        logger.info('[Virtual PSU] Applying Virtual Power (Renode: start)')
        self._send_cmd('machine Reset')
        self._send_cmd('start')
        self._is_on = True

    def disable_output(self) -> None:
        """Translates FSM Power-Off to Renode `pause`."""
        logger.info('[Virtual PSU] Dropping Virtual Power (Renode: pause)')
        self._send_cmd('pause')
        self._is_on = False

    def measure_current(self) -> float:
        """
        Mock implementation for boot profiler inrush current tests.
        Returns a hardcoded "healthy" idle current, or 0.0 if paused.
        """
        if not self._is_on:
            return 0.0
        return 0.45 + time.time() % 0.05

    # ------------------------------------------------------------------
    # Async API (anyio-compatible)
    # ------------------------------------------------------------------

    async def async_connect(self) -> None:
        await anyio.to_thread.run_sync(self.connect)

    async def async_set_voltage(self, volts: float) -> None:
        await anyio.to_thread.run_sync(partial(self.set_voltage, volts))

    async def async_set_current_limit(self, amps: float) -> None:
        await anyio.to_thread.run_sync(partial(self.set_current_limit, amps))

    async def async_enable_output(self) -> None:
        await anyio.to_thread.run_sync(self.enable_output)

    async def async_disable_output(self) -> None:
        await anyio.to_thread.run_sync(self.disable_output)

    async def async_measure_current(self) -> float:
        return await anyio.to_thread.run_sync(self.measure_current)