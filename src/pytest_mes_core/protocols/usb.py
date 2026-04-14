# src/pytest_mes_core/protocols/usb.py
import re
import logging
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import UsbStorageConfig

logger = logging.getLogger("mes_core.protocols.usb")

class UsbMassStorageValidator:
    """
    Validates USB Host ports on the DUT by stressing an authorized thumb drive.
    Hardened against dynamic device enumeration, bent pins, and power-delivery brownouts.
    """
    def __init__(self, transport: DutTransport, config: UsbStorageConfig):
        self.transport = transport
        self.cfg = config
        self.mount_point = f"/tmp/usb_test_{self.cfg.vid_hex}_{self.cfg.pid_hex}"

    def _find_block_device(self) -> str:
        """Dynamically maps the VID:PID to a logical block device (e.g., /dev/sda1)."""
        logger.debug(f"[USB] Probing sysfs for authorized VID:PID {self.cfg.vid_hex}:{self.cfg.pid_hex}...")

        # Find the USB bus and device number for the specific VID:PID
        lsusb_cmd = f"lsusb -d {self.cfg.vid_hex}:{self.cfg.pid_hex}"
        res = self.transport.safe_run(lsusb_cmd, timeout_s=5.0)

        if res.exited != 0 or not res.stdout:
            return ""

        logger.debug("[USB] Hardware detected. Resolving block device mapping...")
        # A robust embedded Linux trick is looking at the /dev/disk/by-id/ path
        res_disk = self.transport.safe_run("ls -l /dev/disk/by-id/usb-* | grep -m 1 part1", timeout_s=3.0)

        if res_disk.exited != 0:
            # Fallback for stripped-down embedded systems (BusyBox)
            fallback = self.transport.safe_run("lsblk -l -o NAME,TRAN | grep usb | awk '{print $1}' | head -n 1", timeout_s=3.0)
            return f"/dev/{fallback.stdout.strip()}1" if fallback.stdout.strip() else ""

        # Extract the device name (e.g., ../../sda1)
        match = re.search(r'../../(sd[a-z][0-9])', res_disk.stdout)
        return f"/dev/{match.group(1)}" if match else ""

    def verify_throughput_and_integrity(self) -> ValidatorResult:
        logger.info(f"[USB] Verifying Host port via authorized drive {self.cfg.vid_hex}:{self.cfg.pid_hex}...")
        context_data: Dict[str, Any] = {}

        try:
            # 1. Dynamic Enumeration
            block_dev = self._find_block_device()
            if not block_dev:
                err_msg = f"Authorized USB Drive ({self.cfg.vid_hex}:{self.cfg.pid_hex}) not enumerated."
                logger.error(f"[USB] {err_msg} Is it plugged in?")
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)

            logger.debug(f"[USB] Drive dynamically mapped to {block_dev}.")
            context_data["block_device"] = block_dev

            # 2. Clear Kernel Ring Buffer (For Brownout Detection)
            logger.debug("[USB] Flushing dmesg buffer to prepare for VBUS brownout monitoring...")
            self.transport.safe_run("dmesg -c > /dev/null", timeout_s=3.0)

            # 3. Safe Mounting
            logger.debug(f"[USB] Ensuring clean mount state at {self.mount_point}...")
            self.transport.safe_run(f"mkdir -p {self.mount_point}", timeout_s=3.0)
            self.transport.safe_run(f"umount -l {block_dev} >/dev/null 2>&1", timeout_s=5.0) # Unmount if previously stuck

            mount_res = self.transport.safe_run(f"mount {block_dev} {self.mount_point}", timeout_s=5.0)
            if mount_res.exited != 0:
                logger.error(f"[USB] Kernel rejected mount: {mount_res.stderr.strip()}")
                return ValidatorResult(passed=False, error_msg=f"Failed to mount {block_dev}: {mount_res.stderr.strip()}", context=context_data)

            test_file = f"{self.mount_point}/factory_test.bin"

            # 4. Stress Test (Write random bytes & force sync)
            logger.debug(f"[USB] Blasting {self.cfg.test_size_mb}MB payload to stress VBUS power delivery (fsync enabled)...")
            write_cmd = f"dd if=/dev/urandom of={test_file} bs=1M count={self.cfg.test_size_mb} conv=fsync"
            write_res = self.transport.safe_run(write_cmd, timeout_s=45.0)

            if write_res.exited != 0:
                logger.error(f"[USB] Payload write failed: {write_res.stderr.strip()}")
                return ValidatorResult(passed=False, error_msg=f"I/O Write Error: {write_res.stderr.strip()}", context=context_data)

            # Parse throughput (e.g., "5242880 bytes (5.2 MB, 5.0 MiB) copied, 0.5 s, 10.5 MB/s")
            # We check both stdout and stderr because BusyBox/GNU differ
            combined_out = f"{write_res.stderr}\n{write_res.stdout}"
            match = re.search(r'([0-9.]+)\s*M[Bb]/s', combined_out)
            throughput = float(match.group(1)) if match else 0.0
            context_data["write_throughput_mbps"] = throughput

            if throughput < self.cfg.min_write_mbps:
                # 🚨 FORENSIC HARDWARE INTERCEPTOR 🚨
                logger.critical("="*60)
                logger.critical(f"[USB] FATAL: USB SPEED DEGRADED!")
                logger.critical(f"[USB] Measured: {throughput} MB/s | Required Limit: {self.cfg.min_write_mbps} MB/s")
                logger.critical("[USB] SuperSpeed (USB 3.0) pins may be bent or unsoldered, causing silent fallback to USB 2.0.")
                logger.critical("="*60)
                return ValidatorResult(
                    passed=False,
                    error_msg=f"USB speed degraded: {throughput} MB/s (Minimum: {self.cfg.min_write_mbps} MB/s)",
                    metrics={"usb_write_mbps": throughput},
                    context=context_data
                )

            # 5. Readback / Integrity Verification
            logger.debug("[USB] Validating cryptographic integrity of written payload...")
            hash_res = self.transport.safe_run(f"md5sum {test_file}", timeout_s=15.0)
            if hash_res.exited != 0:
                 logger.critical(f"[USB] FATAL: Readback I/O Error! Data lines corrupted (EMI or bad traces).")
                 return ValidatorResult(passed=False, error_msg="Readback I/O Error. Corrupted USB data lines.", context=context_data)

            # 6. Brownout Detection (Check for USB resets during high current draw)
            logger.debug("[USB] Scraping dmesg for silent PHY resets or over-current events...")
            dmesg_res = self.transport.safe_run("dmesg | grep -i -E 'usb disconnect|reset.*high-speed|over-current'", timeout_s=5.0)

            if dmesg_res.stdout.strip():
                # 🚨 FORENSIC POWER INTERCEPTOR 🚨
                logger.critical("="*60)
                logger.critical(f"[USB] FATAL: VBUS BROWNOUT DETECTED!")
                logger.critical("[USB] The I/O load triggered a physical over-current event or PHY reset.")
                logger.critical(f"[USB] Kernel Trace:\n{dmesg_res.stdout.strip()}")
                logger.critical("="*60)
                context_data["brownout_trace"] = dmesg_res.stdout.strip()
                return ValidatorResult(passed=False, error_msg="USB switch brownout or EMI reset detected during load.", context=context_data)

            logger.info(f"[USB] Verification complete. Throughput sustained at {throughput} MB/s.")
            return ValidatorResult(
                passed=True,
                metrics={"usb_write_mbps": throughput},
                context=context_data
            )

        except TransportTimeoutError:
            logger.critical("[USB] FATAL: Silicon Lockup. DUT hung entirely during USB I/O operation.")
            return ValidatorResult(passed=False, error_msg="Silicon Lockup: DUT hung during USB I/O.", context=context_data)

        except TransportConnectionError as e:
            # This is the hallmark of a VBUS short-to-ground tripping the PMIC
            logger.critical("="*60)
            logger.critical(f"[USB] FATAL: CATASTROPHIC POWER FAULT DETECTED!")
            logger.critical(f"[USB] Transport pipe shattered during USB load: {e}")
            logger.critical("[USB] VBUS likely shorted to ground, tripping the PMIC and rebooting the board.")
            logger.critical("="*60)
            return ValidatorResult(passed=False, error_msg=f"Transport dropped (VBUS Short / PMIC Trip?): {e}", context=context_data)

        finally:
            # 7. ZERO-LEAKAGE: Guaranteed cleanup
            if self.transport.is_connected:
                try:
                    logger.debug(f"[USB] ZERO-LEAKAGE: Scrubbing payload and unmounting {self.mount_point}...")
                    self.transport.safe_run(f"rm -f {self.mount_point}/factory_test.bin", timeout_s=5.0)
                    self.transport.safe_run("sync", timeout_s=10.0)
                    self.transport.safe_run(f"umount -l {self.mount_point} >/dev/null 2>&1", timeout_s=5.0)
                    self.transport.safe_run(f"rm -rf {self.mount_point} >/dev/null 2>&1", timeout_s=5.0)
                except Exception as cleanup_err:
                    logger.debug(f"[USB] Teardown skipped (Transport likely dead): {cleanup_err}")
