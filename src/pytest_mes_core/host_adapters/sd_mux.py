# src/pytest_mes_core/host_adapters/usb_sd_mux.py
import logging
import subprocess
import time
from typing import Any

from pytest_mes_core.config import UsbSdMuxConfig
from pytest_mes_core.host_adapters.base import BaseHostAdapter, HostAdapterError
from pytest_mes_core.host_adapters.mutex import hardware_mutex, HostMutexTimeoutError

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

    def _set_mux_state(self, state: str) -> None:
        """Helper to invoke the usbsdmux CLI."""
        if state not in ["host", "dut"]:
            raise ValueError("Mux state must be 'host' or 'dut'")

        cmd = ["usbsdmux", self.cfg.serial_id, state]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                raise HostAdapterError(f"usbsdmux failed to switch to {state}: {res.stderr.strip()}")
        except FileNotFoundError:
            raise HostAdapterError("The 'usbsdmux' tool is not installed on the Host PC.")
        except subprocess.TimeoutExpired:
            raise HostAdapterError(f"usbsdmux timed out switching {self.cfg.serial_id} to {state}.")

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
            logger.info(f"[SD-Mux] Toggling {self.cfg.serial_id} to HOST PC...")
            self._set_mux_state("host")

            # 3. Defeat Linux Kernel USB Enumeration Jitter
            # Give the Host PC 2 seconds to enumerate the block device (e.g., /dev/sda)
            time.sleep(2.0)

            return self

        except HostMutexTimeoutError as e:
            raise HostAdapterError(f"Failed to acquire SD-Mux {self.cfg.serial_id}: {e}")

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Flip the SD card back to the DUT and release the lock."""
        try:
            logger.info(f"[SD-Mux] ZERO-LEAKAGE: Toggling {self.cfg.serial_id} back to DUT...")
            self._set_mux_state("dut")
            time.sleep(1.0) # Allow DUT to detect insertion
        except Exception as e:
            logger.error(f"[SD-Mux] Teardown failure on {self.cfg.serial_id}: {e}")
        finally:
            if self._mutex_context:
                self._mutex_context.__exit__(_exc_type, _exc_val, _exc_tb)
                self._mutex_context = None
