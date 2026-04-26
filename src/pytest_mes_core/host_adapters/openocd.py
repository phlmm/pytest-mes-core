import structlog
import logging
from typing import Optional, Any
from pathlib import Path
from pytest_mes_core.host_adapters import BaseHostAdapter, HostAdapterError
from pytest_mes_core.utils import DaemonProcess, DaemonStartupError
logger = structlog.get_logger('mes_core.host_adapters.openocd')

class HostOpenOcdError(HostAdapterError):
    pass

class OpenOcdDaemonAdapter(BaseHostAdapter):

    def __init__(self, interface_cfg: str, target_cfg: str, rpc_port: int=4444):
        self.interface_cfg = interface_cfg
        self.target_cfg = target_cfg
        self.rpc_port = rpc_port
        self._daemon: Optional[DaemonProcess] = None

    def __enter__(self) -> 'OpenOcdDaemonAdapter':
        """Starts the OpenOCD daemon and waits for it to bind the telnet RPC port.

        Returns:
            OpenOcdDaemonAdapter: The active daemon adapter instance.

        Raises:
            HostAdapterError: If OpenOCD is not installed.
            HostOpenOcdError: If the daemon crashes or the JTAG probe is unplugged.
        """
        logger.info('initializing_openocd_daemon_rpc_port_rpc_port', rpc_port=self.rpc_port)
        cmd = ['openocd', '-f', self.interface_cfg, '-f', self.target_cfg, '-c', f'telnet_port {self.rpc_port}', '-c', 'gdb_port disabled', '-c', 'tcl_port disabled']
        try:
            self._daemon = DaemonProcess(cmd=cmd, logger=logger, ready_phrase=f'listening on port {self.rpc_port} for telnet')
            self._daemon.start(timeout_s=5.0)
            logger.info('[JTAG] Hardware locked. OpenOCD Daemon is online.')
            return self
        except DaemonStartupError as e:
            err_msg = f'Failed to bind JTAG probe. Is it plugged in? Error: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            if self._daemon:
                log_path = self._daemon.export_log(Path('/tmp/mes_artifacts'))
                logger.error('openocd_crash_trace_saved_to_log_path', log_path=log_path)
            raise HostOpenOcdError(err_msg)
        except FileNotFoundError:
            raise HostAdapterError('OpenOCD is not installed on the Host PC.')

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Delegate destruction to the Daemon utility."""
        if self._daemon:
            logger.debug('[JTAG] ZERO-LEAKAGE: Releasing JTAG USB Probe.')
            self._daemon.stop()
            self._daemon = None