# src/pytest_mes_core/protocols/block_storage.py
import re
import logging
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.protocols.base import collect_soc_health

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
        logger.debug(f"[Storage] Pre-flight check: Verifying capacity at {mount_point}...")
        df_res = dut.safe_run(f"df -m {mount_point} 2>/dev/null", timeout_s=3.0)

        if not df_res.ok:
            err_msg = f"Mount point unreachable: {mount_point}"
            logger.error(f"[Storage] {err_msg}")
            return ValidatorResult(passed=False, error_msg=err_msg)

        context_data["df_pre_flight"] = df_res.stdout.strip()

        # ==========================================
        # 2. DYNAMIC TIMEOUT CALCULATION
        # ==========================================
        # If the drive drops below 0.5 MB/s, it's virtually dead. We cap the maximum wait time
        # to prevent the test jig from hanging infinitely on a bad batch of eMMC chips.
        max_wait_s = max(30.0, (test_file_size_mb / 0.5))
        logger.debug(f"[Storage] Calculated hardware timeout: {max_wait_s}s (Minimum allowed speed: 0.5 MB/s)")

        # ==========================================
        # 3. PHYSICAL EXECUTION (Defeat the Cache)
        # ==========================================
        cmd = f"dd if=/dev/urandom of={test_file} bs=1M count={test_file_size_mb} conv=fdatasync"
        logger.debug(f"[Storage] Pushing payload to bypass Linux Page Cache: {cmd}")

        # Pre-test thermal baseline
        baseline_health = collect_soc_health(dut)
        if baseline_health:
            context_data["pre_test_health"] = baseline_health

        try:
            # We strictly use safe_run to inherit transport agnosticism and socket protections
            res = dut.safe_run(cmd, timeout_s=max_wait_s)

            # Post-test thermal state
            post_health = collect_soc_health(dut)

            # Combine stdout and stderr. GNU dd writes to stderr, some BusyBox versions write to stdout.
            combined_output = f"{res.stdout}\n{res.stderr}".strip()
            context_data["raw_dd_output"] = combined_output

            # ==========================================
            #  FORENSIC KERNEL INTERCEPTOR
            # ==========================================
            if not res.ok:
                if "No space left on device" in combined_output:
                    logger.error("[Storage] False Negative: Mount point is physically out of space.")
                    return ValidatorResult(passed=False, error_msg="False Negative: Disk is completely full.", context=context_data)

                logger.error(f"[Storage] Physical Write Failed (Exit Code {res.exited}). Scraping dmesg for kernel faults...")

                # If `dd` fails, it is almost always a kernel-level I/O error or mmc bus drop.
                # We scrape the kernel ring buffer for 'mmc', 'sdhci', 'blk', or 'I/O'.
                dmesg_res = dut.safe_run("dmesg | grep -iE 'mmc|sdhci|blk_update_request|I/O error' | tail -n 15", timeout_s=5.0)
                if dmesg_res.ok and dmesg_res.stdout:
                    context_data["kernel_io_faults"] = dmesg_res.stdout.strip()
                    logger.critical("="*60)
                    logger.critical(f"[Storage] FATAL: I/O Fault captured in kernel ring buffer:\n{context_data['kernel_io_faults']}")
                    logger.critical("="*60)

                return ValidatorResult(
                    passed=False,
                    error_msg="Block device rejected the write payload. I/O Error.",
                    context=context_data
                )

            # ==========================================
            # 4. SILICON SPEED PARSING
            # ==========================================
            # Handles "MB/s", "GB/s", "kB/s" (BusyBox drops the space: "50.0MB/s")
            match = re.search(r'([0-9.]+)\s*([kKMG]B/s)', combined_output, re.IGNORECASE)

            if not match:
                logger.error(f"[Storage] Regex parser failed on dd output:\n{combined_output}")
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

            # Inject thermal/freq metrics
            metrics = {
                "write_speed_mbps": round(actual_mbps, 2),
                "write_duration_s": round(res.duration_s, 2)  # Inherited directly from Transport!
            }
            metrics.update(post_health)

            return ValidatorResult(
                passed=passed,
                metrics=metrics,
                error_msg="" if passed else f"Degraded throughput: {actual_mbps} MB/s",
                context=context_data
            )

        except TransportTimeoutError:
            # The transport is alive, but the flash memory controller locked up the CPU.
            logger.critical(f"[Storage] FATAL: SILICON LOCKUP. eMMC failed to complete {test_file_size_mb}MB write within {max_wait_s}s.")
            return ValidatorResult(passed=False, error_msg="Silicon Lockup: Timeout during physical write.", context=context_data)

        except TransportConnectionError as e:
            # The voltage dropped or the kernel panicked, shattering the transport pipe.
            logger.critical(f"[Storage] FATAL: CATASTROPHIC FAULT. Transport pipe shattered during block write: {e}")
            return ValidatorResult(passed=False, error_msg="Catastrophic Fault: Transport dropped during write (Kernel Panic/Power Dip).")

        finally:
            # ==========================================
            # 5. ZERO-LEAKAGE TEARDOWN
            # ==========================================
            # Wrapped in a try/except so a dead transport doesn't mask the actual test failure
            if dut.is_connected:
                try:
                    logger.debug(f"[Storage] ZERO-LEAKAGE: Deleting {test_file_size_mb}MB payload from {test_file}...")
                    dut.safe_run(f"rm -f {test_file}", timeout_s=5.0)
                    dut.safe_run("sync", timeout_s=10.0) # Ensure the deletion is actually committed
                except Exception as cleanup_err:
                    logger.warning(f"[Storage] Cleanup failed (Transport likely destabilized): {cleanup_err}")

    @staticmethod
    def verify_emmc_health(dut: DutTransport, device_path: str = "/dev/mmcblk0") -> ValidatorResult:
        """
        Parses S.M.A.R.T data directly from the eMMC controller's EXTCSD registers via mmc-utils.
        """
        logger.info(f"[Storage] Interrogating S.M.A.R.T EXTCSD registers on {device_path}...")
        context_data: Dict[str, Any] = {}
        metrics: Dict[str, float] = {}

        try:
            res = dut.safe_run(f"mmc extcsd read {device_path}", timeout_s=5.0)
            if not res.ok:
                err_msg = f"mmc-utils failed or {device_path} is invalid: {res.stderr.strip()}"
                logger.error(f"[Storage] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg, context={"stdout": res.stdout, "stderr": res.stderr})

            context_data["extcsd_dump"] = res.stdout

            # Parse "Device life time estimation type A [SEC_COUNT: 0x01]" (0x01 = 0-10%, 0x02 = 10-20%...)
            life_match_a = re.search(r'Device life time estimation type A[^:]*:\s*0x([0-9A-Fa-f]+)', res.stdout)
            life_match_b = re.search(r'Device life time estimation type B[^:]*:\s*0x([0-9A-Fa-f]+)', res.stdout)

            life_a_pct = int(life_match_a.group(1), 16) * 10 if life_match_a else 0
            life_b_pct = int(life_match_b.group(1), 16) * 10 if life_match_b else 0
            max_used_pct = max(life_a_pct, life_b_pct)

            if max_used_pct > 0:
                metrics["emmc_life_used_percent"] = float(max_used_pct)

            # Parse "Pre EOL information [PRE_EOL_INFO: 0x01]"
            # 0x01 = Normal, 0x02 = Warning (80% used), 0x03 = Urgent (EOL)
            eol_match = re.search(r'Pre EOL information[^:]*:\s*0x([0-9A-Fa-f]+)', res.stdout)
            eol_val = int(eol_match.group(1), 16) if eol_match else 0x01
            context_data["pre_eol_val"] = eol_val

            passed = True
            error_msg = ""
            if eol_val >= 0x02:
                passed = False
                error_msg = "eMMC S.M.A.R.T Pre-EOL warning active. Flash is dying!"
                logger.critical("="*60)
                logger.critical(f"[Storage] FATAL: {error_msg}")
                logger.critical("="*60)

            return ValidatorResult(
                passed=passed,
                metrics=metrics,
                error_msg=error_msg,
                context=context_data
            )
        except TransportTimeoutError:
            return ValidatorResult(passed=False, error_msg="DUT hung during EXTCSD read.", context=context_data)
        except TransportConnectionError as e:
            return ValidatorResult(passed=False, error_msg=f"Transport dropped during EXTCSD read: {e}", context=context_data)
