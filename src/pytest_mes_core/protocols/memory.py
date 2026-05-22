import anyio
import structlog
import logging
from typing import Dict, Any
from pytest_mes_core.transports import DutTransport, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
logger = structlog.get_logger('mes_core.protocols.memory')

class NativeMemoryValidator:
    """
    Validates any kernel-abstracted memory device (EEPROM, MTD Block, eMMC)
    using ONLY standard POSIX/Busybox utilities (dd, cat, cmp, sync).
    Zero dependencies required on the target Yocto image.
    """

    @staticmethod
    def verify_full_capacity(dut: DutTransport, device_path: str, total_size_bytes: int, restore_backup: bool=False) -> ValidatorResult:
        """Verifies read/write integrity across the full capacity of a block device.

        Args:
            dut: The transport interface connected to the target.
            device_path: The /dev path of the block device to test.
            total_size_bytes: The total size of the block device in bytes.
            restore_backup: Whether to backup the original contents and restore them
                after the test.

        Returns:
            ValidatorResult: Pass/fail outcome based on a bit-level comparison.
        """
        context_data: Dict[str, Any] = {'device_path': device_path, 'total_size_bytes': total_size_bytes}
        backup_created = False
        logger.info('starting_native_posix_verification_on_device_path_total_size_bytes_bytes', device_path=device_path, total_size_bytes=total_size_bytes)
        if restore_backup:
            logger.info('[Memory] Non-destructive mode enabled. Original data will be backed up.')
        try:
            res_check = dut.safe_run(f'test -e {device_path}')
            if not res_check.ok:
                return ValidatorResult(passed=False, error_msg=f'Device path {device_path} does not exist.', context=context_data)
            if restore_backup:
                logger.debug('backing_up_original_data_from_device_path_to_ram', device_path=device_path)
                backup_cmd = f'dd if={device_path} of=/tmp/original_backup.bin bs=1 count={total_size_bytes} 2>/dev/null'
                res_backup = dut.safe_run(backup_cmd, timeout_s=15.0)
                if not res_backup.ok:
                    return ValidatorResult(passed=False, error_msg=f'Backup failed. Refusing to start destructive test: {res_backup.stderr.strip()}', context=context_data)
                backup_created = True
            logger.debug('[Memory] Generating random test payload...')
            dut.safe_run(f'dd if=/dev/urandom of=/tmp/test_payload.bin bs=1 count={total_size_bytes} 2>/dev/null', timeout_s=5.0)
            logger.debug('streaming_total_size_bytes_bytes_to_silicon_via_vfs', total_size_bytes=total_size_bytes)
            write_cmd = f"sh -c 'cat /tmp/test_payload.bin > {device_path} && sync'"
            res_write = dut.safe_run(write_cmd, timeout_s=15.0)
            if not res_write.ok:
                return ValidatorResult(passed=False, error_msg=f'VFS Write failed: {res_write.stderr.strip()}', context=context_data)
            logger.debug('[Memory] Reading back from silicon...')
            read_cmd = f'dd if={device_path} of=/tmp/readback.bin bs=1 count={total_size_bytes} 2>/dev/null'
            res_read = dut.safe_run(read_cmd, timeout_s=15.0)
            if not res_read.ok:
                return ValidatorResult(passed=False, error_msg=f'VFS Read failed: {res_read.stderr.strip()}', context=context_data)
            logger.debug('[Memory] Verifying bit-integrity...')
            res_cmp = dut.safe_run('cmp -l /tmp/test_payload.bin /tmp/readback.bin', timeout_s=10.0)
            if res_cmp.exited == -3:
                return ValidatorResult(passed=False, error_msg='cmp command timed out. UART dropped keystrokes?', context=context_data)
            elif res_cmp.exited < 0:
                return ValidatorResult(passed=False, error_msg=f'cmp command failed with transport error code: {res_cmp.exited}', context=context_data)
            elif res_cmp.exited != 0:
                error_out = res_cmp.stdout.strip().split('\n')
                error_count = len(error_out)
                logger.critical('=' * 60)
                logger.critical('fatal_error_count_byte_mismatches_detected_on_device_path', error_count=error_count, device_path=device_path)
                logger.critical(f'[Memory] First few errors:\n' + '\n'.join(error_out[:5]))
                logger.critical('=' * 60)
                return ValidatorResult(passed=False, error_msg=f'{error_count} byte mismatches detected.', context=context_data)
            logger.info('success_all_total_size_bytes_bytes_matched_perfectly_on_device_path', total_size_bytes=total_size_bytes, device_path=device_path)
            return ValidatorResult(passed=True, metrics={'t_write_s': res_write.duration_s, 't_read_s': res_read.duration_s}, context=context_data)
        except TransportTimeoutError:
            logger.critical('fatal_dut_hung_during_transaction_bus_locked')
            return ValidatorResult(passed=False, error_msg='DUT hung during memory transaction.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_transport_dropped_brownout_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Transport pipe shattered: {e}', context=context_data)
        finally:
            if dut.is_connected:
                if restore_backup and backup_created:
                    logger.info('test_complete_restoring_original_data_back_to_device_path', device_path=device_path)
                    restore_cmd = f"sh -c 'cat /tmp/original_backup.bin > {device_path} && sync'"
                    try:
                        res_restore = dut.safe_run(restore_cmd, timeout_s=15.0)
                        if not res_restore.ok:
                            logger.critical('=' * 60)
                            logger.critical('catastrophic_failure_could_not_restore_backup_to_device_path', device_path=device_path)
                            logger.critical('error_val', val=res_restore.stderr.strip())
                            logger.critical('=' * 60)
                    except Exception as e:
                        logger.critical('=' * 60)
                        logger.critical('catastrophic_failure_transport_crashed_during_restore_to_device_path', device_path=device_path)
                        logger.critical('exception_e', e=e)
                        logger.critical('=' * 60)
                logger.debug('[Memory] ZERO-LEAKAGE: Cleaning up temporary payloads from DUT RAM...')
                try:
                    dut.safe_run('rm -f /tmp/test_payload.bin /tmp/readback.bin /tmp/original_backup.bin', timeout_s=5.0)
                except Exception:
                    pass

    @staticmethod
    async def async_verify_full_capacity(dut, device_path, total_size_bytes, restore_backup, *args, **kwargs):
        return await anyio.to_thread.run_sync(NativeMemoryValidator.verify_full_capacity, dut, device_path, total_size_bytes, restore_backup, *args, **kwargs)

class RamValidator:
    """
    Validates physical RAM utilizing memtester and hardware EDAC (Error Detection and Correction) registers.
    """

    @staticmethod
    def verify_ram_health(dut: DutTransport, size_mb: int, loops: int=1) -> ValidatorResult:
        """Validates physical RAM integrity using memtester and hardware EDAC counters.

        Args:
            dut: The transport interface connected to the target.
            size_mb: The amount of RAM to test in MB.
            loops: The number of test loops to run.

        Returns:
            ValidatorResult: Pass/fail outcome based on memtester success and zero 
                uncorrectable EDAC errors.
        """
        logger.info('initiating_physical_memory_stress_test_size_mb_mb_for_loops_loops', size_mb=size_mb, loops=loops)
        context_data: Dict[str, Any] = {'size_mb': size_mb, 'loops': loops}
        metrics: Dict[str, float] = {}
        try:
            has_edac = False
            edac_ce_path = '/sys/devices/system/edac/mc/mc0/ce_count'
            edac_ue_path = '/sys/devices/system/edac/mc/mc0/ue_count'
            edac_check = dut.safe_run(f'test -e {edac_ce_path}')
            if edac_check.ok:
                has_edac = True
                logger.debug('[RAM] Hardware EDAC controller detected. Resetting parity fault counters...')
                dut.safe_run(f'echo 0 > {edac_ce_path}')
                dut.safe_run(f'echo 0 > {edac_ue_path}')
            else:
                logger.debug('[RAM] No EDAC controller found. Relying solely on memtester bit-integrity.')
            max_wait_s = max(60.0, size_mb / 100.0 * 10.0 * loops)
            logger.debug('executing_memtester_payload_max_timeout_max_wait_s_s', max_wait_s=max_wait_s)
            res = dut.safe_run(f'memtester {size_mb}M {loops}', timeout_s=max_wait_s)
            context_data['memtester_stdout'] = res.stdout[-1000:] if res.stdout else ''
            if not res.ok:
                err_msg = 'memtester failed or killed by OOM.'
                logger.error('err_msg_exit_code_exited', err_msg=err_msg, exited=res.exited)
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)
            metrics['memtester_passed'] = 1.0
            if has_edac:
                logger.debug('[RAM] Harvesting EDAC parity fault counts post-test...')
                ce_res = dut.safe_run(f'cat {edac_ce_path}', timeout_s=2.0)
                ue_res = dut.safe_run(f'cat {edac_ue_path}', timeout_s=2.0)
                ce_count = int(ce_res.stdout.strip()) if ce_res.ok and ce_res.stdout.strip().isdigit() else 0
                ue_count = int(ue_res.stdout.strip()) if ue_res.ok and ue_res.stdout.strip().isdigit() else 0
                metrics['edac_ce_count'] = float(ce_count)
                metrics['edac_ue_count'] = float(ue_count)
                if ue_count > 0:
                    logger.critical('=' * 60)
                    logger.critical('fatal_ue_count_uncorrectable_edac_ecc_errors_detected_during_stress', ue_count=ue_count)
                    logger.critical('=' * 60)
                    return ValidatorResult(passed=False, metrics=metrics, error_msg='Uncorrectable ECC Errors detected.', context=context_data)
                if ce_count > 0:
                    logger.warning('=' * 60)
                    logger.warning('warning_ce_count_correctable_edac_ecc_errors_detected', ce_count=ce_count)
                    logger.warning('=' * 60)
            logger.info('stress_test_passed_perfectly')
            return ValidatorResult(passed=True, metrics=metrics, context=context_data)
        except TransportTimeoutError:
            logger.critical('[RAM] FATAL: DUT completely locked up during memtester. OOM Killer freeze or bus lockup.')
            return ValidatorResult(passed=False, error_msg='DUT frozen during RAM stress.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_transport_shattered_during_ram_stress_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Transport dropped (OOM reboot?): {e}', context=context_data)
    @staticmethod
    async def async_verify_ram_health(dut, size_mb, loops, *args, **kwargs):
        return await anyio.to_thread.run_sync(RamValidator.verify_ram_health, dut, size_mb, loops, *args, **kwargs)
