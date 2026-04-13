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

from typing import TYPE_CHECKING

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
        # The linter is now perfectly happy because InputDevice is always a valid class
        self.device: Optional[InputDevice] = None

    def __enter__(self) -> 'HeadlessBarcodeScanner':
        if not HAS_EVDEV:
            raise HostAdapterError("evdev library is missing or running on non-Linux OS.")

        target = self.cfg.device_name_substring.lower()

        for path in list_devices():
            try:
                dev = InputDevice(path)
                if dev.name and target in dev.name.lower():
                    logger.info(f"[HID] Bound to scanner '{dev.name}' at {path}")
                    self.device = dev

                    # 1. Exclusively grab the input.
                    self.device.grab()

                    # 2. BUFFER PURGE: Clear any partial keystrokes from premature operator scans
                    while self.device.read_one() is not None:
                        pass

                    return self
            except (IOError, PermissionError) as e:
                logger.warning(f"[HID] Cannot access {path} ({e}). Skipping...")

        logger.error(f"[HID] No scanner matching '{target}' found in /dev/input/.")
        raise HostHardwareDisconnectError(f"HID Scanner '{target}' not found or unplugged.")

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
        """
        Blocks until a full barcode + Carriage Return is scanned,
        or until the configured timeout expires.
        """
        if not self.device:
            raise HostAdapterError("Scanner not initialized. Must be used within a 'with' context manager.")

        logger.info(f"[HID] Awaiting operator scan (Timeout: {self.cfg.scan_timeout_s}s)...")
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
                            logger.info(f"[HID] Scan captured: {barcode}")
                            return barcode

                        if keycode in self.KEY_MAPPING:
                            barcode += self.KEY_MAPPING[keycode]
                        elif keycode.startswith('KEY_') and len(keycode) == 5:
                            barcode += keycode.replace('KEY_', '')

        except OSError as e:
            logger.critical(f"[HID] Hardware disconnect mid-scan: {e}")
            raise HostHardwareDisconnectError(f"Scanner physically disconnected during read operation: {e}")
