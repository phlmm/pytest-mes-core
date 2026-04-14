# src/pytest_mes_core/host_adapters/usb_sd_mux.py
import os
import logging
import subprocess
import time
from typing import Any

from pytest_mes_core.config import UsbSdMuxConfig
from pytest_mes_core.host_adapters import BaseHostAdapter, HostAdapterError
from pytest_mes_core.host_adapters import hardware_mutex, HostMutexTimeoutError

logger = logging.getLogger("mes_core.host_adapters.usb_sd_mux")

class HostUsbSdMuxAdapter(BaseHostAdapter):
    """
    Manages the physical hardware state of a Linux Automation USB-SD-Mux.
    Guarantees the SD card is returned to the DUT (Device Under Test) on teardown.
    """
    def __init__(self, config: UsbSdMuxConfig, mutex_timeout_s: float = 60.0):
        self.cfg = config
        self.mutex_timeout_s = mutex_timeout_s
        self._mutex_context = None

        # ==========================================
        # HARDWARE INTELLIGENCE: Auto-pad to 12 digits
        # ==========================================
        if self.cfg.serial_id.startswith("/dev/"):
            self.device_path = self.cfg.serial_id
        else:
            normalized_serial = self.cfg.serial_id.zfill(12)
            self.device_path = f"/dev/usb-sd-mux/id-{normalized_serial}"

    def _set_mux_state(self, state: str) -> None:
        """Helper to invoke the usbsdmux CLI."""
        if state not in ["host", "dut", "off"]:
            raise ValueError("Mux state must be 'host', 'dut', or 'off'")

        if not os.path.exists(self.device_path):
            err_msg = f"USB-SD-Mux not found at '{self.device_path}'. Is it plugged in and udev rules installed?"
            logger.critical(f"[SD-Mux] FATAL: {err_msg}")
            raise HostAdapterError(err_msg)

        cmd = ["usbsdmux", self.device_path, state]
        logger.debug(f"[SD-Mux] Executing: {' '.join(cmd)}")

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                logger.error(f"[SD-Mux] usbsdmux CLI rejected the command. Stderr: {res.stderr.strip()}")
                raise HostAdapterError(f"usbsdmux failed to switch to {state}: {res.stderr.strip()}")
        except FileNotFoundError:
            err_msg = "The 'usbsdmux' tool is not installed on the Host PC."
            logger.critical(f"[SD-Mux] FATAL: {err_msg}")
            raise HostAdapterError(err_msg)
        except subprocess.TimeoutExpired:
            err_msg = f"usbsdmux timed out switching {self.device_path} to {state}."
            logger.critical(f"[SD-Mux] FATAL: {err_msg}")
            raise HostAdapterError(err_msg)

    def __enter__(self) -> 'HostUsbSdMuxAdapter':
        logger.debug(f"[SD-Mux] Acquiring hardware lock for mux {self.cfg.serial_id}...")

        try:
            # 1. Prevent parallel Pytest workers from colliding on the same Mux
            self._mutex_context = hardware_mutex(
                resource_name=f"sdmux_{self.cfg.serial_id}",
                timeout_s=self.mutex_timeout_s
            )
            self._mutex_context.__enter__()

            # 2. Toggle physical hardware to HOST
            logger.info(f"[SD-Mux] Hardware locked. Toggling {self.device_path} to HOST PC...")
            self._set_mux_state("host")

            # 3. Defeat Linux Kernel USB Enumeration Jitter
            logger.debug("[SD-Mux] Delaying 2.0s for Linux Kernel block device enumeration (/dev/sda)...")
            time.sleep(2.0)

            return self

        except HostMutexTimeoutError as e:
            err_msg = f"Failed to acquire SD-Mux {self.cfg.serial_id}: {e}"
            logger.critical(f"[SD-Mux] FATAL: {err_msg}")
            raise HostAdapterError(err_msg)

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Flip the SD card back to the DUT and release the lock."""
        try:
            logger.info(f"[SD-Mux] ZERO-LEAKAGE: Toggling {self.device_path} back to DUT...")
            self._set_mux_state("dut")
            time.sleep(1.0) # Allow DUT to detect insertion
        except Exception as e:
            # Downgraded to WARNING so we don't accidentally mask a primary test exception
            logger.warning(f"[SD-Mux] Teardown hardware failure on {self.cfg.serial_id}: {e}")
        finally:
            if self._mutex_context:
                logger.debug(f"[SD-Mux] ZERO-LEAKAGE: Releasing OS lock on {self.cfg.serial_id}.")
                self._mutex_context.__exit__(_exc_type, _exc_val, _exc_tb)
                self._mutex_context = None
