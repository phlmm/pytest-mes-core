import structlog
import os
import select
import logging
from typing import Optional, Any, List, TYPE_CHECKING

class _DummyInputDevice:
    name: str
    path: str
    fd: int

    def grab(self) -> None:
        pass

    def ungrab(self) -> None:
        pass

    def read_one(self) -> Any:
        pass

    def read(self) -> Any:
        pass

    def close(self) -> None:
        pass

class _DummyEcodes:
    EV_KEY: int = 1
try:
    from evdev import InputDevice, categorize, ecodes, list_devices
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False
    InputDevice = _DummyInputDevice
    ecodes = _DummyEcodes()

    def list_devices() -> List[str]:
        return []

    def categorize(event: Any) -> Any:
        return None
from pytest_mes_core.config import HidScannerConfig
from pytest_mes_core.host_adapters.base import BaseHostAdapter, HostAdapterError, HostHardwareDisconnectError
logger = structlog.get_logger('mes_core.host_adapters.hid')

class HidScannerTimeoutError(HostAdapterError):
    """Raised when the operator fails to scan within the time limit."""
    pass

class HeadlessBarcodeScanner(BaseHostAdapter):
    """
    Captures physical USB Barcode Scanners in the background.
    Bypasses the OS keyboard buffer, sanitizes stale inputs,
    and complies with the zero-leakage BaseHostAdapter contract.
    """
    KEY_MAPPING = {'KEY_0': '0', 'KEY_1': '1', 'KEY_2': '2', 'KEY_3': '3', 'KEY_4': '4', 'KEY_5': '5', 'KEY_6': '6', 'KEY_7': '7', 'KEY_8': '8', 'KEY_9': '9', 'KEY_MINUS': '-', 'KEY_EQUAL': '=', 'KEY_SPACE': ' ', 'KEY_DOT': '.', 'KEY_SLASH': '/', 'KEY_BACKSLASH': '\\', 'KEY_SEMICOLON': ':'}

    def __init__(self, cfg: HidScannerConfig):
        self.cfg = cfg
        self.device: Optional[InputDevice] = None

    def __enter__(self) -> 'HeadlessBarcodeScanner':
        if not HAS_EVDEV:
            raise HostAdapterError('evdev library is missing or running on non-Linux OS.')
        target = self.cfg.device_name_substring.lower()
        logger.debug('hunting_for_scanner_matching_target_in_dev_input', target=target)
        for path in list_devices():
            try:
                dev = InputDevice(path)
                if dev.name and target in dev.name.lower():
                    logger.info('hardware_bound_name_at_path', name=dev.name, path=path)
                    self.device = dev
                    self.device.grab()
                    purged_count = 0
                    while self.device.read_one() is not None:
                        purged_count += 1
                    if purged_count > 0:
                        logger.debug('purged_purged_count_stale_keystrokes_from_hardware_buffer', purged_count=purged_count)
                    return self
            except (IOError, PermissionError) as e:
                logger.debug('cannot_access_path_e_skipping', path=path, e=e)
        err_msg = f"HID Scanner '{target}' not found or unplugged."
        logger.critical('fatal_err_msg', err_msg=err_msg)
        raise HostHardwareDisconnectError(err_msg)

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Release the kernel lock on the USB device."""
        if self.device:
            logger.debug('zero_leakage_ungrabbing_scanner_path', path=self.device.path)
            try:
                self.device.ungrab()
            except Exception:
                pass
            finally:
                self.device.close()
                self.device = None

    def wait_for_scan(self) -> str:
        """Blocks until a full barcode + Carriage Return is scanned.

        Uses POSIX select to implement a non-blocking wait with a strict timeout,
        capturing physical keystrokes directly from the evdev input buffer.

        Returns:
            str: The fully parsed barcode string.

        Raises:
            HostAdapterError: If the scanner is not initialized.
            HidScannerTimeoutError: If the operator fails to scan within the timeout.
            HostHardwareDisconnectError: If the scanner is unplugged mid-scan.
        """
        if not self.device:
            raise HostAdapterError("Scanner not initialized. Must be used within a 'with' context manager.")
        logger.warning('operator_action_scan_barcode_now_timeout_scan_timeout_s_s', scan_timeout_s=self.cfg.scan_timeout_s)
        barcode = ''
        try:
            while True:
                r, _, _ = select.select([self.device.fd], [], [], self.cfg.scan_timeout_s)
                if not r:
                    logger.error('operator_failed_to_scan_within_scan_timeout_s_s', scan_timeout_s=self.cfg.scan_timeout_s)
                    raise HidScannerTimeoutError(f'Barcode scan timed out after {self.cfg.scan_timeout_s}s.')
                for event in self.device.read():
                    if event.type == ecodes.EV_KEY and event.value == 1:
                        key = categorize(event)
                        keycode = key.keycode[0] if isinstance(key.keycode, list) else key.keycode
                        if keycode == 'KEY_ENTER':
                            logger.info('scan_successfully_captured_barcode', barcode=barcode)
                            return barcode
                        if keycode in self.KEY_MAPPING:
                            char = self.KEY_MAPPING[keycode]
                            logger.debug('rx_keycode_char', keycode=keycode, char=char)
                            barcode += char
                        elif keycode.startswith('KEY_') and len(keycode) == 5:
                            char = keycode.replace('KEY_', '')
                            logger.debug('rx_keycode_char', keycode=keycode, char=char)
                            barcode += char
                        else:
                            logger.debug('rx_keycode_unmapped_ignored', keycode=keycode)
        except OSError as e:
            logger.critical('hardware_disconnect_mid_scan_e', e=e)
            raise HostHardwareDisconnectError(f'Scanner physically disconnected during read operation: {e}')

    async def async_wait_for_scan(self) -> str:
        """Async wrapper for wait_for_scan()."""
        import anyio
        return await anyio.to_thread.run_sync(self.wait_for_scan)