# src/pytest_mes_core/host_adapters/hid_scanner.py
import os
import select
import logging
from typing import Optional, Any, List, TYPE_CHECKING

# ==========================================
# CROSS-PLATFORM STATIC TYPING STUBS
# ==========================================
# Evdev is Linux-only. We use dummy stubs to satisfy strict Pylance/MyPy
# type checkers when developing/linting on Windows or macOS.

class _DummyInputDevice:
        name: str
        path: str
        fd: int
        def grab(self) -> None: pass
        def ungrab(self) -> None: pass
        def read_one(self) -> Any: pass
        def read(self) -> Any: pass
        def close(self) -> None: pass

class _DummyEcodes:
        EV_KEY: int = 1
try:
    from evdev import InputDevice, categorize, ecodes, list_devices
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False
    InputDevice = _DummyInputDevice                   # type: ignore
    ecodes = _DummyEcodes()                           # type: ignore
    def list_devices() -> List[str]: return []        # type: ignore
    def categorize(event: Any) -> Any: return None    # type: ignore


from pytest_mes_core.config import HidScannerConfig
from pytest_mes_core.host_adapters.base import (
    BaseHostAdapter,
    HostAdapterError,
    HostHardwareDisconnectError
)

logger = logging.getLogger("mes_core.host_adapters.hid")

class HidScannerTimeoutError(HostAdapterError):
    """Raised when the operator fails to scan within the time limit."""
    pass

class HeadlessBarcodeScanner(BaseHostAdapter):
    """
    Captures physical USB Barcode Scanners in the background.
    Bypasses the OS keyboard buffer, sanitizes stale inputs,
    and complies with the zero-leakage BaseHostAdapter contract.
    """

    KEY_MAPPING = {
        'KEY_0': '0', 'KEY_1': '1', 'KEY_2': '2', 'KEY_3': '3', 'KEY_4': '4',
        'KEY_5': '5', 'KEY_6': '6', 'KEY_7': '7', 'KEY_8': '8', 'KEY_9': '9',
        'KEY_MINUS': '-', 'KEY_EQUAL': '=', 'KEY_SPACE': ' ', 'KEY_DOT': '.',
        'KEY_SLASH': '/', 'KEY_BACKSLASH': '\\', 'KEY_SEMICOLON': ':'
    }

    def __init__(self, cfg: HidScannerConfig):
        self.cfg = cfg
        self.device: Optional[InputDevice] = None

    def __enter__(self) -> 'HeadlessBarcodeScanner':
        if not HAS_EVDEV:
            raise HostAdapterError("evdev library is missing or running on non-Linux OS.")

        target = self.cfg.device_name_substring.lower()
        logger.debug(f"[HID] Hunting for scanner matching '{target}' in /dev/input/...")

        for path in list_devices():
            try:
                dev = InputDevice(path)
                if dev.name and target in dev.name.lower():
                    logger.info(f"[HID] Hardware bound: '{dev.name}' at {path}")
                    self.device = dev

                    # 1. Exclusively grab the input.
                    self.device.grab()

                    # 2. BUFFER PURGE: Clear any partial keystrokes from premature operator scans
                    purged_count = 0
                    while self.device.read_one() is not None:
                        purged_count += 1

                    if purged_count > 0:
                        logger.debug(f"[HID] Purged {purged_count} stale keystrokes from hardware buffer.")

                    return self
            except (IOError, PermissionError) as e:
                # Log at DEBUG because X11/Wayland aggressively locks keyboards and mice,
                # causing expected permission errors on standard desktop inputs.
                logger.debug(f"[HID] Cannot access {path} ({e}). Skipping...")

        # If we exit the loop, the scanner wasn't found
        err_msg = f"HID Scanner '{target}' not found or unplugged."
        logger.critical(f"[HID] FATAL: {err_msg}")
        raise HostHardwareDisconnectError(err_msg)

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Release the kernel lock on the USB device."""
        if self.device:
            logger.debug(f"[HID] ZERO-LEAKAGE: Ungrabbing scanner {self.device.path}.")
            try:
                self.device.ungrab()
            except Exception:
                pass # Device might have already been physically unplugged
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

        # Always visible Operator prompt
        logger.warning(f">>> [OPERATOR ACTION] SCAN BARCODE NOW (Timeout: {self.cfg.scan_timeout_s}s) <<<")
        barcode = ""

        try:
            while True:
                # DEFENSIVE: Use select to wait for I/O readiness with a strict timeout
                r, _, _ = select.select([self.device.fd], [], [], self.cfg.scan_timeout_s)

                if not r:
                    logger.error(f"[HID] Operator failed to scan within {self.cfg.scan_timeout_s}s.")
                    raise HidScannerTimeoutError(f"Barcode scan timed out after {self.cfg.scan_timeout_s}s.")

                for event in self.device.read():
                    if event.type == ecodes.EV_KEY and event.value == 1:
                        key = categorize(event)
                        keycode = key.keycode[0] if isinstance(key.keycode, list) else key.keycode

                        if keycode == 'KEY_ENTER':
                            logger.info(f"[HID] Scan successfully captured: '{barcode}'")
                            return barcode

                        if keycode in self.KEY_MAPPING:
                            char = self.KEY_MAPPING[keycode]
                            logger.debug(f"[HID] RX: {keycode} -> '{char}'")
                            barcode += char
                        elif keycode.startswith('KEY_') and len(keycode) == 5:
                            char = keycode.replace('KEY_', '')
                            logger.debug(f"[HID] RX: {keycode} -> '{char}'")
                            barcode += char
                        else:
                            logger.debug(f"[HID] RX: {keycode} (Unmapped/Ignored)")

        except OSError as e:
            logger.critical(f"[HID] Hardware disconnect mid-scan: {e}")
            raise HostHardwareDisconnectError(f"Scanner physically disconnected during read operation: {e}")
