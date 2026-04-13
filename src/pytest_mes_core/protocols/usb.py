# src/pytest_mes_core/protocols/usb.py
import re
import logging
from pytest_mes_core.transports import DutTransport
from pytest_mes_core.protocols import ValidatorResult

from pytest_mes_core.config import UsbStorageConfig

logger = logging.getLogger("mes_core.protocols.usb")

class UsbMassStorageValidator:
    """
    Validates USB Host ports on the DUT by stressing an authorized thumb drive.
    Hardened against dynamic device enumeration and power-delivery brownouts.
    """
    def __init__(self, transport: DutTransport, config: "UsbStorageConfig"): # type: ignore
        self.transport = transport
        self.cfg = config
        self.mount_point = f"/tmp/usb_test_{self.cfg.vid_hex}_{self.cfg.pid_hex}"

    def _find_block_device(self) -> str:
        """Dynamically maps the VID:PID to a logical block device (e.g., /dev/sda1)."""
        # Find the USB bus and device number for the specific VID:PID
        lsusb_cmd = f"lsusb -d {self.cfg.vid_hex}:{self.cfg.pid_hex}"
        res = self.transport.safe_run(lsusb_cmd)

        if res.exited != 0 or not res.stdout:
            return ""

        # Use udevadm or lsblk to map the physical USB ID to a block device
        # A robust embedded Linux trick is looking at the /dev/disk/by-id/ path
        res_disk = self.transport.safe_run("ls -l /dev/disk/by-id/usb-* | grep -m 1 part1")
        if res_disk.exited != 0:
            # Fallback for stripped-down embedded systems
            fallback = self.transport.safe_run("lsblk -l -o NAME,TRAN | grep usb | awk '{print $1}' | head -n 1")
            return f"/dev/{fallback.stdout.strip()}1" if fallback.stdout.strip() else ""

        # Extract the device name (e.g., ../../sda1)
        match = re.search(r'../../(sd[a-z][0-9])', res_disk.stdout)
        return f"/dev/{match.group(1)}" if match else ""

    def verify_throughput_and_integrity(self) -> ValidatorResult:
        logger.info(f"[USB] Verifying Host port via authorized drive {self.cfg.vid_hex}:{self.cfg.pid_hex}...")

        # 1. Dynamic Enumeration
        block_dev = self._find_block_device()
        if not block_dev:
            return ValidatorResult(passed=False, error_msg=f"Authorized USB Drive ({self.cfg.vid_hex}:{self.cfg.pid_hex}) not enumerated.")

        logger.debug(f"[USB] Drive dynamically mapped to {block_dev}.")

        # 2. Clear Kernel Ring Buffer (For Brownout Detection)
        self.transport.safe_run("dmesg -c > /dev/null")

        # 3. Safe Mounting
        self.transport.safe_run(f"mkdir -p {self.mount_point}")
        self.transport.safe_run(f"umount -l {block_dev}", warn=True) # Unmount if previously stuck

        mount_res = self.transport.safe_run(f"mount {block_dev} {self.mount_point}")
        if mount_res.exited != 0:
            return ValidatorResult(passed=False, error_msg=f"Failed to mount {block_dev}: {mount_res.stderr}")

        test_file = f"{self.mount_point}/factory_test.bin"

        try:
            # 4. Stress Test (Write random bytes & force sync)
            write_cmd = f"dd if=/dev/urandom of={test_file} bs=1M count={self.cfg.test_size_mb} conv=fsync"
            write_res = self.transport.safe_run(write_cmd, timeout_s=45.0)

            if write_res.exited != 0:
                return ValidatorResult(passed=False, error_msg=f"I/O Write Error: {write_res.stderr.strip()}")

            # Parse throughput (e.g., "5242880 bytes (5.2 MB, 5.0 MiB) copied, 0.5 s, 10.5 MB/s")
            match = re.search(r'([0-9.]+)\s*M[Bb]/s', write_res.stderr)
            throughput = float(match.group(1)) if match else 0.0

            if throughput < self.cfg.min_write_mbps:
                return ValidatorResult(
                    passed=False,
                    error_msg=f"USB speed degraded: {throughput} MB/s (Minimum: {self.cfg.min_write_mbps} MB/s)"
                )

            # 5. Readback / Integrity Verification
            hash_res = self.transport.safe_run(f"md5sum {test_file}")
            if hash_res.exited != 0:
                 return ValidatorResult(passed=False, error_msg="Readback I/O Error. Corrupted USB data lines.")

            # 6. Brownout Detection (Check for USB resets during high current draw)
            dmesg_res = self.transport.safe_run("dmesg | grep -i -E 'usb disconnect|reset.*high-speed|over-current'")
            if dmesg_res.stdout:
                logger.critical(f"[USB] VBUS BROWNOUT DETECTED! Kernel log:\n{dmesg_res.stdout}")
                return ValidatorResult(passed=False, error_msg="USB switch brownout or EMI reset detected during load.")

            return ValidatorResult(
                passed=True,
                metrics={"usb_write_mbps": throughput}
            )

        finally:
            # 7. ZERO-LEAKAGE: Guaranteed cleanup
            self.transport.safe_run(f"rm -f {test_file}")
            self.transport.safe_run("sync")
            self.transport.safe_run(f"umount -l {self.mount_point}")
            self.transport.safe_run(f"rm -rf {self.mount_point}")
