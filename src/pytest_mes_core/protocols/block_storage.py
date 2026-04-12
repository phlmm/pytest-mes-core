# src/pytest_mes_core/protocols/block_storage.py
import time
import re
import logging
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.protocols.block_storage")

class BlockDeviceValidator:
    """Validates eMMC and SD Card interfaces, defeating Linux RAM page caches."""

    @staticmethod
    def measure_throughput(
        dut_ssh: EphemeralSSHClient,
        mount_point: str = "/mnt/sdcard",
        test_file_size_mb: int = 50,
        min_write_mbps: float = 10.0
    ) -> ValidatorResult:
        """
        Hardware Flow:
            Writes a massive block of zeros to the target filesystem.
            Uses `conv=fdatasync` to force the Linux kernel to flush the Page Cache
            to the actual NAND flash before returning, yielding true silicon speed.
        """
        test_file = f"{mount_point}/.mes_eol_speed_test.bin"
        logger.info(f"[Storage] Initiating {test_file_size_mb}MB physical write test to {mount_point}...")

        # 1. Defeat the cache and force a physical sync
        cmd = f"dd if=/dev/zero of={test_file} bs=1M count={test_file_size_mb} conv=fdatasync"

        t0 = time.perf_counter()
        res = dut_ssh.conn.run(cmd, hide=True, warn=True)
        duration = time.perf_counter() - t0

        try:
            # 2. Parse dd stderr output (dd puts stats in stderr)
            # Example target: "52428800 bytes (52 MB, 50 MiB) copied, 2.34 s, 22.4 MB/s"
            output = res.stderr.strip()
            match = re.search(r'([0-9.]+)\s*(MB/s|GB/s)', output)

            if not match:
                logger.error(f"[Storage] Failed to parse dd metrics: {output}")
                return ValidatorResult(passed=False, error_msg="Could not parse throughput metrics.")

            speed_val = float(match.group(1))
            unit = match.group(2)

            # Normalize to MB/s
            actual_mbps = speed_val * 1024.0 if unit == "GB/s" else speed_val

        finally:
            # 3. ZERO-LEAKAGE: Destroy the massive test payload so we don't ship full disks
            logger.debug(f"[Storage] ZERO-LEAKAGE: Deleting 50MB payload from {test_file}")
            dut_ssh.conn.run(f"rm -f {test_file}", hide=True, warn=True)
            # Sync the deletion to disk
            dut_ssh.conn.run("sync", hide=True, warn=True)

        logger.info(f"[Storage] True Silicon Write Speed: {actual_mbps} MB/s (Required: >{min_write_mbps})")

        passed = actual_mbps >= min_write_mbps
        if not passed:
            logger.warning(f"[Storage] THROUGHPUT FAILED. Counterfeit or degraded flash memory detected.")

        return ValidatorResult(
            passed=passed,
            metrics={"write_speed_mbps": round(actual_mbps, 2), "write_duration_s": round(duration, 2)},
            error_msg="" if passed else f"Degraded throughput: {actual_mbps} MB/s"
        )
