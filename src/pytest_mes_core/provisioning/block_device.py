# src/pytest_mes_core/provisioning/block_device.py
import os
import stat
import logging
import subprocess
from pathlib import Path

from pytest_mes_core.provisioning.base import (
    BaseProvisioner,
    ProvisioningError,
    ImageVerificationError
)

# IMPORT THE NEW ENTERPRISE PRIMITIVE
from pytest_mes_core.utils.process import LiveProcess, ProcessTimeoutError, ProcessExecutionError

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
        logger.debug(f"[Provisioning] Executing pre-flight safety checks on {self.host_block_device}...")

        if not os.path.exists(self.host_block_device):
            err_msg = (
                f"Block device {self.host_block_device} does not exist. "
                "Is the SD Mux toggled to Host mode? Is the USB unplugged?"
            )
            logger.critical(f"[Provisioning] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)

        # 1. Ensure it's actually a raw block device, not a standard file or directory
        mode = os.stat(self.host_block_device).st_mode
        if not stat.S_ISBLK(mode):
            err_msg = (
                f"SECURITY TRIP: '{self.host_block_device}' is NOT a block device! "
                "Aborting flash to prevent catastrophic Host OS destruction."
            )
            logger.critical("="*60)
            logger.critical(f"[Provisioning] FATAL {err_msg}")
            logger.critical("="*60)
            raise ProvisioningError(err_msg)

        # 2. Ensure the Host Linux Kernel hasn't auto-mounted the target's partitions
        # Writing raw bytes to a mounted filesystem corrupts the kernel VFS.
        try:
            with open("/proc/mounts", "r") as f:
                mounts = f.read()
                # Check for /dev/sdb AND partitions like /dev/sdb1, /dev/sdb2
                if self.host_block_device in mounts:
                    logger.warning(f"[Provisioning] {self.host_block_device} is currently mounted. Attempting unmount...")

                    umount_cmd = f"umount {self.host_block_device}*"
                    logger.debug(f"[Provisioning] Executing: {umount_cmd}")
                    # Fast, silent programmatic commands stay as subprocess.run
                    subprocess.run(umount_cmd, shell=True, stderr=subprocess.DEVNULL)

                    # Re-verify
                    with open("/proc/mounts", "r") as f2:
                        if self.host_block_device in f2.read():
                            err_msg = f"Failed to unmount {self.host_block_device}. Device is busy (Check 'lsof')."
                            logger.critical(f"[Provisioning] FATAL: {err_msg}")
                            raise ProvisioningError(err_msg)
                else:
                    logger.debug(f"[Provisioning] OS confirms {self.host_block_device} is unmounted and free.")
        except FileNotFoundError:
            logger.debug("[Provisioning] /proc/mounts not found. Assuming non-Linux Host OS.")

    def provision(self, image_path: Path) -> None:
        """
        Hardware Flow:
            Uses bmaptool to securely and rapidly flash an image to physical media.
            Enforces a final POSIX 'sync' to flush RAM caches to silicon.
        """
        if not image_path.exists():
            err_msg = f"Firmware image missing at {image_path}"
            logger.critical(f"[Provisioning] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)

        # 1. Execute Safety Firewall
        self._pre_flight_safety_check()

        logger.info(f"[Provisioning] Initiating bmaptool flash of {image_path.name} to {self.host_block_device}...")

        cmd = ["bmaptool", "copy", str(image_path), self.host_block_device]

        try:
            # ==========================================
            # LIVE PROCESS EXECUTION & TELEMETRY
            # ==========================================
            process = LiveProcess(cmd, self.timeout_s, logger).execute()

            stdout_lower = process.stdout.lower()

            if process.returncode != 0:
                # EXPORT ARTIFACT: Dump the exact failure trace to a file for CI/CD retrieval
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.error(f"\n[Provisioning] bmaptool failed with code {process.returncode}! Full trace saved to {log_path}")

                # Check for common BMAP errors
                if "no such file" in stdout_lower and ".bmap" in stdout_lower:
                    raise ProvisioningError("bmaptool requires a .bmap file next to the image, but it is missing.")
                elif "permission denied" in stdout_lower:
                    raise ProvisioningError("Permission denied. Pytest must be run with sudo/root for block level access.")
                else:
                    raise ProvisioningError(f"bmaptool execution failed with code {process.returncode}.")

            # 2. Defeat OS Caching (Critical for USB-SD-Mux before switching it back to DUT)
            logger.info("\n[Provisioning] Flash successful. Forcing kernel sync to flush RAM buffers to silicon...")
            subprocess.run(["sync"], check=True)

            logger.info(f"[Provisioning] Image successfully provisioned and synced in {process.duration_s}s.")

        except ProcessTimeoutError:
            raise ProvisioningError("Block device flash timed out. Is the SD card physically defective?")
        except ProcessExecutionError as e:
            err_msg = f"Failed to execute OS command. Is bmaptool installed? Error: {e}"
            logger.critical(f"[Provisioning] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)
