# src/pytest_mes_core/provisioning/block_device.py
import os
import stat
import time
import logging
import subprocess
from pathlib import Path

from pytest_mes_core.provisioning import (
    BaseProvisioner,
    ProvisioningError,
    ImageVerificationError
)

logger = logging.getLogger("mes_core.provisioning.block_device")

class BmapBlockDeviceProvisioner(BaseProvisioner):
    """
    Flashes physical block devices connected to the Host PC (via USB-SD-Mux or direct USB).
    Strictly enforces OS-level safety checks to prevent catastrophic Host PC destruction.
    """
    def __init__(self, host_block_device: str, timeout_s: int = 300):
        self.host_block_device = host_block_device
        self.timeout_s = timeout_s

    def _pre_flight_safety_check(self) -> None:
        """Mathematically verifies the target is a valid, unmounted block device."""
        if not os.path.exists(self.host_block_device):
            raise ProvisioningError(
                f"Block device {self.host_block_device} does not exist. "
                "Is the SD Mux toggled to Host mode? Is the USB unplugged?"
            )

        # 1. Ensure it's actually a raw block device, not a standard file or directory
        mode = os.stat(self.host_block_device).st_mode
        if not stat.S_ISBLK(mode):
            raise ProvisioningError(
                f"FATAL SECURITY TRIP: '{self.host_block_device}' is not a block device! "
                "Aborting flash to prevent Host OS destruction."
            )

        # 2. Ensure the Host Linux Kernel hasn't auto-mounted the target's partitions
        # Writing raw bytes to a mounted filesystem corrupts the kernel VFS.
        try:
            with open("/proc/mounts", "r") as f:
                mounts = f.read()
                # Check for /dev/sdb AND partitions like /dev/sdb1, /dev/sdb2
                if self.host_block_device in mounts:
                    logger.warning(f"[Provisioning] {self.host_block_device} is currently mounted. Attempting unmount...")
                    # We use umount '**' to catch all partitions
                    subprocess.run(f"umount {self.host_block_device}*", shell=True, stderr=subprocess.DEVNULL)

                    # Re-verify
                    with open("/proc/mounts", "r") as f2:
                        if self.host_block_device in f2.read():
                            raise ProvisioningError(f"Failed to unmount {self.host_block_device}. Device is busy.")
        except FileNotFoundError:
            pass # Non-Linux OS fallback

    def provision(self, image_path: Path) -> None:
        """
        Hardware Flow:
            Uses bmaptool to securely and rapidly flash an image to physical media.
            Enforces a final POSIX 'sync' to flush RAM caches to silicon.
        """
        if not image_path.exists():
            raise ProvisioningError(f"Firmware image missing at {image_path}")

        # 1. Execute Safety Firewall
        self._pre_flight_safety_check()

        logger.info(f"[Provisioning] Initiating bmaptool flash of {image_path.name} to {self.host_block_device}...")

        # bmaptool automatically handles .bmap file resolution and handles sync internally,
        # but we capture output to map errors cleanly to our domain exceptions.
        cmd = ["bmaptool", "copy", str(image_path), self.host_block_device]
        t0 = time.perf_counter()

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s)
            stdout_lower = res.stdout.lower() + res.stderr.lower()

            if res.returncode != 0:
                logger.error(f"[Provisioning] bmaptool failed:\n{res.stderr}")

                # Check for common BMAP errors
                if "no such file" in stdout_lower and ".bmap" in stdout_lower:
                    raise ProvisioningError("bmaptool requires a .bmap file next to the image, but it is missing.")
                elif "permission denied" in stdout_lower:
                    raise ProvisioningError("Permission denied. Pytest must be run with sudo for block level access.")
                else:
                    raise ProvisioningError(f"bmaptool execution failed with code {res.returncode}.")

            # 2. Defeat OS Caching (Critical for USB-SD-Mux before switching it back to DUT)
            logger.debug("[Provisioning] Forcing kernel sync to flush buffers to physical SD silicon...")
            subprocess.run(["sync"], check=True)

            duration = round(time.perf_counter() - t0, 3)
            logger.info(f"[Provisioning] Flash completed and synced in {duration}s.")

        except subprocess.TimeoutExpired:
            logger.critical(f"[Provisioning] bmaptool flash TIMEOUT after {self.timeout_s}s.")
            raise ProvisioningError(f"Block device flash timed out. Is the SD card physically defective?")
        except FileNotFoundError:
            raise ProvisioningError("bmaptool is not installed on the Host PC.")
