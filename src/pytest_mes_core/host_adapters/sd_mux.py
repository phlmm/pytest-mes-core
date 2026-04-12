import time
import subprocess
import logging
from tenacity import retry, stop_after_attempt, wait_fixed
from pathlib import Path

logger = logging.getLogger("mes_core.host_adapters.sd_mux")

class HostUsbSdMux:
    """Zero-leakage manager for the Linux Automation USB-SD-Mux."""

    def __init__(self, serial_id: str, host_block_device: str):
        self.serial_id = serial_id
        self.host_block_device = Path(host_block_device)

    def switch_to_host(self) -> None:
        """Flips the SD card to the Host PC and waits for Linux udev enumeration."""
        logger.info(f"[SD-Mux {self.serial_id}] Flipping SD Card to Host PC...")
        cmd = ["usbsdmux", self.serial_id, "host"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FATAL: Failed to command SD-Mux. Output: {res.stderr}")

        self._wait_for_block_device(timeout_s=10)

    def switch_to_dut(self) -> None:
        """Flips the SD card to the Device Under Test."""
        logger.info(f"[SD-Mux {self.serial_id}] Flipping SD Card to DUT...")
        cmd = ["usbsdmux", self.serial_id, "dut"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FATAL: Failed to command SD-Mux. Output: {res.stderr}")
        time.sleep(0.5) # Let DUT detect the insertion event

    @retry(stop=stop_after_attempt(20), wait=wait_fixed(0.5), reraise=True)
    def _wait_for_block_device(self, timeout_s: int) -> None:
        """Tenacity polling loop to defeat OS udev enumeration jitter."""
        if not self.host_block_device.exists():
            logger.debug(f"[SD-Mux] Waiting for {self.host_block_device} to enumerate...")
            raise FileNotFoundError(f"Block device {self.host_block_device} not found yet.")
        logger.info(f"[SD-Mux] Block device {self.host_block_device} enumerated successfully.")

    def __enter__(self) -> 'HostUsbSdMux':
        # Default starting state: connected to host for provisioning
        self.switch_to_host()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """ZERO-LEAKAGE: Always hand the SD card back to the DUT on teardown."""
        logger.debug(f"[SD-Mux {self.serial_id}] Executing Zero-Leakage Teardown...")
        self.switch_to_dut()
