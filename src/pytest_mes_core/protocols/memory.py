import logging
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult

logger = logging.getLogger("mes_core.protocols.memory")

class NativeMemoryValidator:
    """
    Validates any kernel-abstracted memory device (EEPROM, MTD Block, eMMC)
    using ONLY standard POSIX/Busybox utilities (dd, cat, cmp, sync).
    Zero dependencies required on the target Yocto image.
    """

    @staticmethod
    def verify_full_capacity(
        dut: DutTransport,
        device_path: str,
        total_size_bytes: int,
        restore_backup: bool = False
    ) -> ValidatorResult:

        context_data: Dict[str, Any] = {"device_path": device_path, "total_size_bytes": total_size_bytes}
        backup_created = False

        logger.info(f"[Memory] Starting Native POSIX verification on {device_path} ({total_size_bytes} bytes)...")
        if restore_backup:
            logger.info("[Memory] Non-destructive mode enabled. Original data will be backed up.")

        try:
            # 1. Verification of device presence
            res_check = dut.safe_run(f"test -e {device_path}")
            if not res_check.ok:
                return ValidatorResult(passed=False, error_msg=f"Device path {device_path} does not exist.", context=context_data)

            # ==========================================
            # PHASE: BACKUP
            # ==========================================
            if restore_backup:
                logger.debug(f"[Memory] Backing up original data from {device_path} to RAM...")
                backup_cmd = f"dd if={device_path} of=/tmp/original_backup.bin bs=1 count={total_size_bytes} 2>/dev/null"
                res_backup = dut.safe_run(backup_cmd, timeout_s=15.0)

                if not res_backup.ok:
                    return ValidatorResult(passed=False, error_msg=f"Backup failed. Refusing to start destructive test: {res_backup.stderr.strip()}", context=context_data)

                backup_created = True

            # ==========================================
            # PHASE: TEST EXECUTION
            # ==========================================
            # 2. Generate random payload directly on the DUT's RAM (/tmp)
            logger.debug("[Memory] Generating random test payload...")
            dut.safe_run(f"dd if=/dev/urandom of=/tmp/test_payload.bin bs=1 count={total_size_bytes} 2>/dev/null", timeout_s=5.0)

            # 3. Write payload to hardware
            logger.debug(f"[Memory] Streaming {total_size_bytes} bytes to silicon via VFS...")
            write_cmd = f"sh -c 'cat /tmp/test_payload.bin > {device_path} && sync'"
            res_write = dut.safe_run(write_cmd, timeout_s=15.0)

            if not res_write.ok:
                return ValidatorResult(passed=False, error_msg=f"VFS Write failed: {res_write.stderr.strip()}", context=context_data)

            # 4. Read back from hardware
            logger.debug("[Memory] Reading back from silicon...")
            read_cmd = f"dd if={device_path} of=/tmp/readback.bin bs=1 count={total_size_bytes} 2>/dev/null"
            res_read = dut.safe_run(read_cmd, timeout_s=15.0)

            if not res_read.ok:
                return ValidatorResult(passed=False, error_msg=f"VFS Read failed: {res_read.stderr.strip()}", context=context_data)

            # 5. Bit-level comparison
            logger.debug("[Memory] Verifying bit-integrity...")
            res_cmp = dut.safe_run("cmp -l /tmp/test_payload.bin /tmp/readback.bin", timeout_s=10.0)

            if res_cmp.exited != 0:
                error_out = res_cmp.stdout.strip().split('\n')
                error_count = len(error_out)
                logger.critical("="*60)
                logger.critical(f"[Memory] FATAL: {error_count} byte mismatches detected on {device_path}!")
                logger.critical(f"[Memory] First few errors:\n" + "\n".join(error_out[:5]))
                logger.critical("="*60)
                return ValidatorResult(passed=False, error_msg=f"{error_count} byte mismatches detected.", context=context_data)

            logger.info(f"[Memory] SUCCESS: All {total_size_bytes} bytes matched perfectly on {device_path}!")
            return ValidatorResult(
                passed=True,
                metrics={"t_write_s": res_write.duration_s, "t_read_s": res_read.duration_s},
                context=context_data
            )

        except TransportTimeoutError:
            logger.critical(f"[Memory] FATAL: DUT hung during transaction. Bus locked?")
            return ValidatorResult(passed=False, error_msg="DUT hung during memory transaction.", context=context_data)
        except TransportConnectionError as e:
            logger.critical(f"[Memory] FATAL: Transport dropped (Brownout?): {e}")
            return ValidatorResult(passed=False, error_msg=f"Transport pipe shattered: {e}", context=context_data)

        finally:
            # ==========================================
            # ZERO-LEAKAGE TEARDOWN & RESTORE
            # ==========================================
            if dut.is_connected:
                # THE SAFETY NET: Always attempt to restore the backup safely
                if restore_backup and backup_created:
                    logger.info(f"[Memory] Test complete. Restoring original data back to {device_path}...")
                    restore_cmd = f"sh -c 'cat /tmp/original_backup.bin > {device_path} && sync'"

                    try:
                        res_restore = dut.safe_run(restore_cmd, timeout_s=15.0)
                        if not res_restore.ok:
                            logger.critical("="*60)
                            logger.critical(f"[Memory] CATASTROPHIC FAILURE: COULD NOT RESTORE BACKUP TO {device_path}!")
                            logger.critical(f"[Memory] Error: {res_restore.stderr.strip()}")
                            logger.critical("="*60)
                    except Exception as e:
                        logger.critical("="*60)
                        logger.critical(f"[Memory] CATASTROPHIC FAILURE: Transport crashed during restore to {device_path}!")
                        logger.critical(f"[Memory] Exception: {e}")
                        logger.critical("="*60)

                logger.debug("[Memory] ZERO-LEAKAGE: Cleaning up temporary payloads from DUT RAM...")
                try:
                    dut.safe_run("rm -f /tmp/test_payload.bin /tmp/readback.bin /tmp/original_backup.bin", timeout_s=5.0)
                except Exception:
                    pass # Ignore cleanup errors if transport is dead

class RamValidator:
    """
    Validates physical RAM utilizing memtester and hardware EDAC (Error Detection and Correction) registers.
    """

    @staticmethod
    def verify_ram_health(dut: DutTransport, size_mb: int, loops: int = 1) -> ValidatorResult:
        logger.info(f"[RAM] Initiating physical memory stress test: {size_mb}MB for {loops} loops...")
        context_data: Dict[str, Any] = {"size_mb": size_mb, "loops": loops}
        metrics: Dict[str, float] = {}

        try:
            # 1. Clear EDAC counters (if they exist) so we only measure faults during the test
            has_edac = False
            edac_ce_path = "/sys/devices/system/edac/mc/mc0/ce_count"
            edac_ue_path = "/sys/devices/system/edac/mc/mc0/ue_count"

            edac_check = dut.safe_run(f"test -e {edac_ce_path}")
            if edac_check.ok:
                has_edac = True
                logger.debug("[RAM] Hardware EDAC controller detected. Resetting parity fault counters...")
                dut.safe_run(f"echo 0 > {edac_ce_path}")
                dut.safe_run(f"echo 0 > {edac_ue_path}")
            else:
                logger.debug("[RAM] No EDAC controller found. Relying solely on memtester bit-integrity.")

            # 2. Run memtester
            # Note: memtester will run exactly {loops} times and then exit cleanly
            # We assign a generous timeout: assume ~10 seconds per 100MB per loop
            max_wait_s = max(60.0, (size_mb / 100.0) * 10.0 * loops)
            logger.debug(f"[RAM] Executing memtester payload (Max timeout: {max_wait_s}s)...")
            
            # Use safe_run to capture the return code and stdout
            res = dut.safe_run(f"memtester {size_mb}M {loops}", timeout_s=max_wait_s)
            
            context_data["memtester_stdout"] = res.stdout[-1000:] if res.stdout else ""
            
            if not res.ok:
                err_msg = "memtester failed or killed by OOM."
                logger.error(f"[RAM] {err_msg} Exit code: {res.exited}")
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)

            metrics["memtester_passed"] = 1.0

            # 3. Harvest EDAC Faults
            if has_edac:
                logger.debug("[RAM] Harvesting EDAC parity fault counts post-test...")
                ce_res = dut.safe_run(f"cat {edac_ce_path}", timeout_s=2.0)
                ue_res = dut.safe_run(f"cat {edac_ue_path}", timeout_s=2.0)

                ce_count = int(ce_res.stdout.strip()) if ce_res.ok and ce_res.stdout.strip().isdigit() else 0
                ue_count = int(ue_res.stdout.strip()) if ue_res.ok and ue_res.stdout.strip().isdigit() else 0

                metrics["edac_ce_count"] = float(ce_count)
                metrics["edac_ue_count"] = float(ue_count)

                if ue_count > 0:
                    logger.critical("="*60)
                    logger.critical(f"[RAM] FATAL: {ue_count} Uncorrectable EDAC ECC Errors detected during stress!")
                    logger.critical("="*60)
                    return ValidatorResult(passed=False, metrics=metrics, error_msg="Uncorrectable ECC Errors detected.", context=context_data)

                if ce_count > 0:
                    logger.warning("="*60)
                    logger.warning(f"[RAM] WARNING: {ce_count} Correctable EDAC ECC Errors detected.")
                    logger.warning("="*60)

            logger.info(f"[RAM] Stress test passed perfectly.")
            return ValidatorResult(passed=True, metrics=metrics, context=context_data)

        except TransportTimeoutError:
            logger.critical("[RAM] FATAL: DUT completely locked up during memtester. OOM Killer freeze or bus lockup.")
            return ValidatorResult(passed=False, error_msg="DUT frozen during RAM stress.", context=context_data)
        except TransportConnectionError as e:
            logger.critical(f"[RAM] FATAL: Transport shattered during RAM stress. {e}")
            return ValidatorResult(passed=False, error_msg=f"Transport dropped (OOM reboot?): {e}", context=context_data)
