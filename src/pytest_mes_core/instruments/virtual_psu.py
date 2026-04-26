import structlog
import telnetlib
import logging
import time
from typing import Optional
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
        self._tn: Optional[telnetlib.Telnet] = None
        self._is_on = False

    def connect(self) -> None:
        """Connects to the Renode Monitor port via Telnet."""
        logger.debug('connecting_to_renode_monitor_at_host_monitor_port', host=self.host, monitor_port=self.monitor_port)
        try:
            self._tn = telnetlib.Telnet(self.host, self.monitor_port, timeout=5)
            self._tn.read_until(b'(machine-0)', timeout=2)
            logger.info('[Virtual PSU] Connected to Renode Simulation Monitor.')
        except ConnectionRefusedError:
            logger.critical('fatal_could_not_connect_to_renode_monitor_at_host_monitor_port', host=self.host, monitor_port=self.monitor_port)
            logger.critical('Is the RenodeRunner active?')
            raise

    def _send_cmd(self, cmd: str) -> str:
        """Sends a command to the Renode monitor and reads the response."""
        if not self._tn:
            return ''
        logger.debug('tx_cmd', cmd=cmd)
        self._tn.write(f'{cmd}\n'.encode('ascii'))
        resp = self._tn.read_until(b'(machine-0)', timeout=2).decode('utf-8', errors='ignore')
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