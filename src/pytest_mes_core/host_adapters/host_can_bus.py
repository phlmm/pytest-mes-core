import structlog
import time
import logging
from typing import Optional, Any
try:
    import can
except ImportError:
    can = None
from pytest_mes_core.config import HostCanConfig
from pytest_mes_core.host_adapters.base import BaseHostAdapter, HostAdapterError
logger = structlog.get_logger('mes_core.host_adapters.can')

class HostCanAdapter(BaseHostAdapter):
    """
    Natively bridges 'socketcan' and 'slcan' USB dongles.
    Complies with the BaseHostAdapter Zero-Leakage contract via __enter__/__exit__.
    """

    def __init__(self, cfg: HostCanConfig):
        self.cfg = cfg
        self.bus: Optional['can.BusABC'] = None
        self.listener: Optional['can.BufferedReader'] = None
        self.notifier: Optional['can.Notifier'] = None

    def __enter__(self) -> 'HostCanAdapter':
        self.connect()
        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Stop the background Notifier thread and release the CAN socket."""
        self.disconnect()

    def connect(self) -> None:
        """Initializes the physical CAN adapter via python-can.

        Binds the adapter using the configured bustype (e.g., 'slcan' or 'socketcan').
        For socketcan interfaces, attempts to natively bring the network interface UP.

        Raises:
            HostAdapterError: If python-can is missing, the interface is down, or binding fails.
        """
        if can is None:
            raise HostAdapterError('python-can package is not installed.')
        if self.bus:
            return
        logger.info('binding_bustype_on_interface_bitrate_bps', bustype=self.cfg.bustype, interface=self.cfg.interface, bitrate=self.cfg.bitrate)
        kwargs = {'interface': self.cfg.bustype, 'channel': self.cfg.interface, 'bitrate': self.cfg.bitrate}
        if self.cfg.bustype == 'slcan':
            kwargs['tty_baudrate'] = self.cfg.tty_baudrate
        elif self.cfg.bustype == 'socketcan':
            import subprocess
            import os
            subprocess.run(['sudo', '-n', 'ip', 'link', 'set', self.cfg.interface, 'down'], capture_output=True)
            subprocess.run(['sudo', '-n', 'ip', 'link', 'set', self.cfg.interface, 'type', 'can', 'bitrate', str(self.cfg.bitrate)], capture_output=True)
            subprocess.run(['sudo', '-n', 'ip', 'link', 'set', self.cfg.interface, 'up'], capture_output=True)
            sysfs_path = f'/sys/class/net/{self.cfg.interface}/operstate'
            if os.path.exists(sysfs_path):
                with open(sysfs_path, 'r') as f:
                    state = f.read().strip()
                if state == 'down':
                    raise HostAdapterError(f"FATAL: CAN Interface '{self.cfg.interface}' is DOWN. The automated 'sudo ip link set up' command failed (likely due to sudo password prompt). Please bring the interface up manually: sudo ip link set {self.cfg.interface} up")
        try:
            self.bus = can.Bus(**kwargs)
            self.listener = can.BufferedReader()
            self.notifier = can.Notifier(self.bus, [self.listener])
        except Exception as e:
            self.disconnect()
            raise HostAdapterError(f'Failed to bind CAN bus {self.cfg.interface}: {e}')

    def disconnect(self) -> None:
        """Safely shuts down the CAN bus and releases any background polling threads."""
        if self.notifier:
            try:
                self.notifier.stop()
            except Exception as e:
                logger.debug('notifier_stop_failed_e', e=e)
            self.notifier = None
        if self.bus:
            try:
                self.bus.shutdown()
            except Exception as e:
                logger.debug('bus_shutdown_failed_e', e=e)
            self.bus = None
        self.listener = None

    def clear_rx_buffer(self) -> None:
        """Flushes any stale frames from the adapter's RX buffer before starting a test."""
        if self.listener:
            while self.listener.get_message(timeout=0.0):
                pass

    def send(self, can_id: int, payload: bytes) -> None:
        """Transmits a standard CAN frame onto the physical bus.

        Args:
            can_id: The standard arbitration ID (11-bit).
            payload: The binary payload to transmit.

        Raises:
            HostAdapterError: If the bus is not connected.
        """
        if not self.bus:
            raise HostAdapterError('Host CAN not connected.')
        msg = can.Message(arbitration_id=can_id, data=payload, is_extended_id=False)
        self.bus.send(msg)

    def expect(self, expected_id: int, expected_payload: bytes, timeout_s: float=2.0) -> bool:
        """Blocks until a matching CAN frame is received or the timeout expires.

        Args:
            expected_id: The exact arbitration ID to match.
            expected_payload: The exact payload data to match.
            timeout_s: Maximum time to wait in seconds.

        Returns:
            bool: True if the exact frame was captured, False otherwise.

        Raises:
            HostAdapterError: If the bus is not connected.
        """
        if not self.listener:
            raise HostAdapterError('Host CAN not connected.')
        t_end = time.perf_counter() + timeout_s
        while time.perf_counter() < t_end:
            msg = self.listener.get_message(timeout=0.1)
            if msg and msg.arbitration_id == expected_id and (bytes(msg.data) == expected_payload):
                return True
        return False