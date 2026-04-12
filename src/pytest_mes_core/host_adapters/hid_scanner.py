# src/pytest_mes_core/hid_scanner.py
import evdev # type: ignore
import select
import logging
from typing import Optional
from pytest_mes_core.config import HidScannerConfig

logger = logging.getLogger("mes_core.hid_scanner")

class HeadlessBarcodeScanner:
    """
    Captures physical USB Barcode Scanners in the background.
    Bypasses the OS keyboard buffer to prevent operators from scanning serial numbers
    into random Linux terminal windows.
    """
    def __init__(self, cfg: HidScannerConfig):
        self.cfg = cfg
        self.device: Optional[evdev.InputDevice] = None

    def __enter__(self) -> 'HeadlessBarcodeScanner':
        target = self.cfg.device_name_substring.lower()

        for path in evdev.list_devices():
            dev = evdev.InputDevice(path)
            if target in dev.name.lower():
                logger.info(f"[HID] Bound to scanner '{dev.name}' at {path}")
                self.device = dev
                try:
                    self.device.grab() # Exclusively grab. OS ignores it as a keyboard.
                except IOError as e:
                    logger.error(f"[HID] Failed to exclusively grab {path}. Already in use? {e}")
                    raise RuntimeError(f"FATAL: HID Scanner '{dev.name}' is busy.")
                return self

        logger.error(f"[HID] No scanner matching '{target}' found in /dev/input/.")
        raise RuntimeError(f"FATAL: HID Scanner '{target}' not found.")

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """ZERO-LEAKAGE: Release the kernel lock on the USB device."""
        if self.device:
            logger.debug(f"[HID] ZERO-LEAKAGE: Ungrabbing scanner {self.device.path}.")
            try:
                self.device.ungrab()
            except Exception:
                pass
            self.device.close()

    def wait_for_scan(self) -> str:
        """
        Blocks until a full barcode + Carriage Return is scanned,
        or until the configured timeout expires.
        """
        if not self.device:
            raise RuntimeError("Scanner not initialized. Use a 'with' context manager.")

        logger.info(f"[HID] Awaiting operator scan (Timeout: {self.cfg.scan_timeout_s}s)...")
        barcode = ""

        while True:
            # DEFENSIVE: Use select to wait for I/O readiness with a strict timeout
            r, w, x = select.select([self.device.fd], [], [], self.cfg.scan_timeout_s)

            if not r:
                logger.error(f"[HID] Operator failed to scan within {self.cfg.scan_timeout_s}s.")
                raise TimeoutError(f"Barcode scan timed out after {self.cfg.scan_timeout_s}s.")

            for event in self.device.read():
                if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                    key = evdev.categorize(event)

                    if key.keycode == 'KEY_ENTER':
                        logger.info(f"[HID] Scan captured: {barcode}")
                        return barcode

                    # Note: Production systems might need a more complex mapping dictionary
                    # here to handle Shift characters, but for raw alphanumerics this suffices.
                    char = key.keycode.replace('KEY_', '')
                    barcode += char
