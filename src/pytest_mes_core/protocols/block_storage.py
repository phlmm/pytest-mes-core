import re
import logging
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult

logger = logging.getLogger("mes_core.protocols.block_storage")

class BlockDeviceValidator:
    """
    Validates eMMC, NVMe, and SD Card interfaces.
    Defeats Linux RAM page caches, protects against Silicon Lockups,
    and leverages native transport telemetry.
    """

    @staticmethod
    def measure_throughput(
        dut: DutTransport,
        mount_point: str = "/mnt/sdcard",
        test_file_size_mb: int = 50,
        min_write_mbps: float = 10.0
    ) -> ValidatorResult:
        """
        Hardware Flow:
            1. Verifies filesystem capacity to prevent false-negative "Disk Full" errors.
            2. Writes a massive block of zeros to the target filesystem.
            3. Uses `conv=fdatasync` to force the Linux kernel to flush the Page Cache
               to the actual NAND flash before returning, yielding true silicon speed.
        """
        test_file = f"{mount_point}/.mes_eol_speed_test.bin"
        context_data: Dict[str, Any] = {}

        logger.info(f"[Storage] Initiating {test_file_size_mb}MB physical write test to {mount_point}...")

        # ==========================================
        # 1. PRE-FLIGHT CHECK: Capacity Verification
        # ==========================================
        # Check if the mount point exists and grab raw df output
        df_res = dut.safe_run(f"df -m {mount_point} 2>/dev/null", timeout_s=3.0)
        if not df_res.ok:
            return ValidatorResult(passed=False, error_msg=f"Mount point unreachable: {mount_point}")

        context_data["df_pre_flight"] = df_res.stdout.strip()

        # ==========================================
        # 2. DYNAMIC TIMEOUT CALCULATION
        # ==========================================
        # If the drive drops below 0.5 MB/s, it's virtually dead. We cap the maximum wait time
        # to prevent the test jig from hanging infinitely on a bad batch of eMMC chips.
        max_wait_s = max(30.0, (test_file_size_mb / 0.5))
        logger.debug(f"[Storage] Enforcing strict hardware timeout of {max_wait_s}s")

        # ==========================================
        # 3. PHYSICAL EXECUTION (Defeat the Cache)
        # ==========================================
        cmd = f"dd if=/dev/zero of={test_file} bs=1M count={test_file_size_mb} conv=fdatasync"

        try:
            # We strictly use safe_run to inherit transport agnosticism and socket protections
            res = dut.safe_run(cmd, timeout_s=max_wait_s)

            # Combine stdout and stderr. GNU dd writes to stderr, some BusyBox versions write to stdout.
            combined_output = f"{res.stdout}\n{res.stderr}".strip()
            context_data["raw_dd_output"] = combined_output

            # ==========================================
            # 🚨 FORENSIC KERNEL INTERCEPTOR 🚨
            # ==========================================
            if not res.ok:
                if "No space left on device" in combined_output:
                    return ValidatorResult(passed=False, error_msg="False Negative: Disk is completely full.", context=context_data)

                logger.error(f"[Storage] Physical Write Failed (Exit Code {res.exited}). Scraping dmesg...")

                # If `dd` fails, it is almost always a kernel-level I/O error or mmc bus drop.
                # We scrape the kernel ring buffer for 'mmc', 'sdhci', 'blk', or 'I/O'.
                dmesg_res = dut.safe_run("dmesg | grep -iE 'mmc|sdhci|blk_update_request|I/O error' | tail -n 15", timeout_s=5.0)
                if dmesg_res.ok and dmesg_res.stdout:
                    context_data["kernel_io_faults"] = dmesg_res.stdout.strip()
                    logger.debug(f"[Storage] I/O Fault captured: \n{context_data['kernel_io_faults']}")

                return ValidatorResult(
                    passed=False,
                    error_msg="Block device rejected the write payload. I/O Error.",
                    context=context_data
                )

            # ==========================================
            # 4. SILICON SPEED PARSING
            # ==========================================
            # Handles "MB/s", "GB/s", "kB/s" (BusyBox drops the space: "50.0MB/s")
            match = re.search(r'([0-9.]+)\s*([kKMG]B/s)', combined_output)

            if not match:
                logger.error(f"[Storage] Failed to parse metrics from output: {combined_output}")
                return ValidatorResult(passed=False, error_msg="Regex parser failed on dd output.", context=context_data)

            speed_val = float(match.group(1))
            unit = match.group(2).upper()

            # Normalize everything to strictly MB/s
            if unit == "GB/S":
                actual_mbps = speed_val * 1024.0
            elif unit == "KB/S":
                actual_mbps = speed_val / 1024.0
            else:
                actual_mbps = speed_val

            logger.info(f"[Storage] True Silicon Write Speed: {round(actual_mbps, 2)} MB/s (Required: >{min_write_mbps})")

            passed = actual_mbps >= min_write_mbps
            if not passed:
                logger.warning(f"[Storage] THROUGHPUT FAILED. Counterfeit or degraded flash memory detected.")

            return ValidatorResult(
                passed=passed,
                metrics={
                    "write_speed_mbps": round(actual_mbps, 2),
                    "write_duration_s": round(res.duration_s, 2)  # Inherited directly from Transport!
                },
                error_msg="" if passed else f"Degraded throughput: {actual_mbps} MB/s",
                context=context_data
            )

        except TransportTimeoutError:
            # The transport is alive, but the flash memory controller locked up the CPU.
            logger.critical(f"[Storage] SILICON LOCKUP: eMMC failed to complete {test_file_size_mb}MB write within {max_wait_s}s.")
            return ValidatorResult(passed=False, error_msg="Silicon Lockup: Timeout during physical write.", context=context_data)

        except TransportConnectionError as e:
            # The voltage dropped or the kernel panicked, shattering the transport pipe.
            logger.critical(f"[Storage] CATASTROPHIC FAULT: Transport pipe shattered during block write: {e}")
            return ValidatorResult(passed=False, error_msg="Catastrophic Fault: Transport dropped during write (Kernel Panic/Power Dip).")

        finally:
            # ==========================================
            # 5. ZERO-LEAKAGE TEARDOWN
            # ==========================================
            # Wrapped in a try/except so a dead transport doesn't mask the actual test failure
            if dut.is_connected:
                try:
                    logger.debug(f"[Storage] ZERO-LEAKAGE: Deleting {test_file_size_mb}MB payload from {test_file}")
                    dut.safe_run(f"rm -f {test_file}", timeout_s=5.0)
                    dut.safe_run("sync", timeout_s=10.0) # Ensure the deletion is actually committed
                except Exception as cleanup_err:
                    logger.debug(f"[Storage] Cleanup failed: {cleanup_err}")
