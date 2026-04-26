import structlog
import os
import logging
import subprocess
import time
from typing import Any
from pytest_mes_core.config import UsbSdMuxConfig
from pytest_mes_core.host_adapters import BaseHostAdapter, HostAdapterError
from pytest_mes_core.host_adapters import hardware_mutex, HostMutexTimeoutError
logger = structlog.get_logger('mes_core.host_adapters.usb_sd_mux')

class HostUsbSdMuxAdapter(BaseHostAdapter):
    """
    Manages the physical hardware state of a Linux Automation USB-SD-Mux.
    Guarantees the SD card is returned to the DUT (Device Under Test) on teardown.
    """

    def __init__(self, config: UsbSdMuxConfig, mutex_timeout_s: float=60.0):
        self.cfg = config
        self.mutex_timeout_s = mutex_timeout_s
        self._mutex_context = None
        if self.cfg.serial_id.startswith('/dev/'):
            self.device_path = self.cfg.serial_id
        else:
            normalized_serial = self.cfg.serial_id.zfill(12)
            self.device_path = f'/dev/usb-sd-mux/id-{normalized_serial}'

    def _set_mux_state(self, state: str) -> None:
        """Helper to invoke the usbsdmux CLI."""
        if state not in ['host', 'dut', 'off']:
            raise ValueError("Mux state must be 'host', 'dut', or 'off'")
        if not os.path.exists(self.device_path):
            err_msg = f"USB-SD-Mux not found at '{self.device_path}'. Is it plugged in and udev rules installed?"
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)
        cmd = ['usbsdmux', self.device_path, state]
        logger.debug('executing_val', val=' '.join(cmd))
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                logger.error('usbsdmux_cli_rejected_the_command_stderr_val', val=res.stderr.strip())
                raise HostAdapterError(f'usbsdmux failed to switch to {state}: {res.stderr.strip()}')
        except FileNotFoundError:
            err_msg = "The 'usbsdmux' tool is not installed on the Host PC."
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)
        except subprocess.TimeoutExpired:
            err_msg = f'usbsdmux timed out switching {self.device_path} to {state}.'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)

    def __enter__(self) -> 'HostUsbSdMuxAdapter':
        """Acquires a cross-process lock and toggles the SD Mux to the Host PC.

        Includes a defensive 2-second sleep to ensure the Linux kernel completes
        USB block device enumeration before returning.

        Returns:
            HostUsbSdMuxAdapter: The locked and host-connected SD Mux adapter.

        Raises:
            HostAdapterError: If the OS lock fails or the usbsdmux command fails.
        """
        logger.debug('acquiring_hardware_lock_for_mux_serial_id', serial_id=self.cfg.serial_id)
        try:
            self._mutex_context = hardware_mutex(resource_name=f'sdmux_{self.cfg.serial_id}', timeout_s=self.mutex_timeout_s)
            self._mutex_context.__enter__()
            logger.info('hardware_locked_toggling_device_path_to_host_pc', device_path=self.device_path)
            self._set_mux_state('host')
            logger.debug('[SD-Mux] Delaying 2.0s for Linux Kernel block device enumeration...')
            time.sleep(2.0)
            return self
        except HostMutexTimeoutError as e:
            err_msg = f'Failed to acquire SD-Mux {self.cfg.serial_id}: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Flip the SD card back to the DUT and release the lock."""
        try:
            logger.info('zero_leakage_toggling_device_path_back_to_dut', device_path=self.device_path)
            self._set_mux_state('dut')
            time.sleep(1.0)
        except Exception as e:
            logger.warning('teardown_hardware_failure_on_serial_id_e', serial_id=self.cfg.serial_id, e=e)
        finally:
            if self._mutex_context:
                logger.debug('zero_leakage_releasing_os_lock_on_serial_id', serial_id=self.cfg.serial_id)
                self._mutex_context.__exit__(_exc_type, _exc_val, _exc_tb)
                self._mutex_context = None