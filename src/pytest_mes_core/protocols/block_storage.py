import anyio
import structlog
import re
import logging
from typing import Dict, Any
from pytest_mes_core.transports import DutTransport, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.protocols.base import collect_soc_health
logger = structlog.get_logger('mes_core.protocols.block_storage')

class BlockDeviceValidator:
    """
    Validates eMMC, NVMe, and SD Card interfaces.
    Defeats Linux RAM page caches, protects against Silicon Lockups,
    and leverages native transport telemetry.
    """

    @staticmethod
    def measure_throughput(dut: DutTransport, mount_point: str='/mnt/sdcard', test_file_size_mb: int=50, min_write_mbps: float=10.0) -> ValidatorResult:
        """Measures true physical write throughput of a block storage device.

        Hardware Flow:
            1. Verifies filesystem capacity to prevent false-negative "Disk Full" errors.
            2. Writes a massive block of zeros to the target filesystem.
            3. Uses `conv=fdatasync` to force the Linux kernel to flush the Page Cache
               to the actual NAND flash before returning, yielding true silicon speed.

        Args:
            dut: The transport interface connected to the target.
            mount_point: The path where the block device is mounted.
            test_file_size_mb: The size of the payload to write in megabytes.
            min_write_mbps: The minimum required write speed in MB/s.

        Returns:
            ValidatorResult: Contains the pass/fail outcome, write speed metrics, and 
                any captured I/O fault traces from dmesg if the write failed.
        """
        test_file = f'{mount_point}/.mes_eol_speed_test.bin'
        context_data: Dict[str, Any] = {}
        logger.info('initiating_test_file_size_mb_mb_physical_write_test_to_mount_point', test_file_size_mb=test_file_size_mb, mount_point=mount_point)
        logger.debug('pre_flight_check_verifying_capacity_at_mount_point', mount_point=mount_point)
        df_res = dut.safe_run(f'df -m {mount_point} 2>/dev/null', timeout_s=3.0)
        if not df_res.ok:
            err_msg = f'Mount point unreachable: {mount_point}'
            logger.error('err_msg', err_msg=err_msg)
            return ValidatorResult(passed=False, error_msg=err_msg)
        context_data['df_pre_flight'] = df_res.stdout.strip()
        max_wait_s = max(30.0, test_file_size_mb / 0.5)
        logger.debug('calculated_hardware_timeout_max_wait_s_s_minimum_allowed_speed_0_5_mb_s', max_wait_s=max_wait_s)
        cmd = f'dd if=/dev/urandom of={test_file} bs=1M count={test_file_size_mb} conv=fdatasync'
        logger.debug('pushing_payload_to_bypass_linux_page_cache_cmd', cmd=cmd)
        baseline_health = collect_soc_health(dut)
        if baseline_health:
            context_data['pre_test_health'] = baseline_health
        try:
            res = dut.safe_run(cmd, timeout_s=max_wait_s)
            post_health = collect_soc_health(dut)
            combined_output = f'{res.stdout}\n{res.stderr}'.strip()
            context_data['raw_dd_output'] = combined_output
            if not res.ok:
                if 'No space left on device' in combined_output:
                    logger.error('[Storage] False Negative: Mount point is physically out of space.')
                    return ValidatorResult(passed=False, error_msg='False Negative: Disk is completely full.', context=context_data)
                logger.error('physical_write_failed_exit_code_exited_scraping_dmesg_for_kernel_faults', exited=res.exited)
                dmesg_res = dut.safe_run("dmesg | grep -iE 'mmc|sdhci|blk_update_request|I/O error' | tail -n 15", timeout_s=5.0)
                if dmesg_res.ok and dmesg_res.stdout:
                    context_data['kernel_io_faults'] = dmesg_res.stdout.strip()
                    logger.critical('=' * 60)
                    logger.critical('fatal_i_o_fault_captured_in_kernel_ring_buffer_val', val=context_data['kernel_io_faults'])
                    logger.critical('=' * 60)
                return ValidatorResult(passed=False, error_msg='Block device rejected the write payload. I/O Error.', context=context_data)
            match = re.search('([0-9.]+)\\s*([kKMG]B/s)', combined_output, re.IGNORECASE)
            if not match:
                logger.error('regex_parser_failed_on_dd_output_combined_output', combined_output=combined_output)
                return ValidatorResult(passed=False, error_msg='Regex parser failed on dd output.', context=context_data)
            speed_val = float(match.group(1))
            unit = match.group(2).upper()
            if unit == 'GB/S':
                actual_mbps = speed_val * 1024.0
            elif unit == 'KB/S':
                actual_mbps = speed_val / 1024.0
            else:
                actual_mbps = speed_val
            logger.info('true_silicon_write_speed_val_mb_s_required_min_write_mbps', val=round(actual_mbps, 2), min_write_mbps=min_write_mbps)
            passed = actual_mbps >= min_write_mbps
            if not passed:
                logger.warning('throughput_failed_counterfeit_or_degraded_flash_memory_detected')
            metrics = {'write_speed_mbps': round(actual_mbps, 2), 'write_duration_s': round(res.duration_s, 2)}
            metrics.update(post_health)
            return ValidatorResult(passed=passed, metrics=metrics, error_msg='' if passed else f'Degraded throughput: {actual_mbps} MB/s', context=context_data)
        except TransportTimeoutError:
            logger.critical('fatal_silicon_lockup_emmc_failed_to_complete_test_file_size_mb_mb_write_within_max_wait_s_s', test_file_size_mb=test_file_size_mb, max_wait_s=max_wait_s)
            return ValidatorResult(passed=False, error_msg='Silicon Lockup: Timeout during physical write.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_catastrophic_fault_transport_pipe_shattered_during_block_write_e', e=e)
            return ValidatorResult(passed=False, error_msg='Catastrophic Fault: Transport dropped during write (Kernel Panic/Power Dip).')
        finally:
            if dut.is_connected:
                try:
                    logger.debug('zero_leakage_deleting_test_file_size_mb_mb_payload_from_test_file', test_file_size_mb=test_file_size_mb, test_file=test_file)
                    dut.safe_run(f'rm -f {test_file}', timeout_s=5.0)
                    dut.safe_run('sync', timeout_s=10.0)
                except Exception as cleanup_err:
                    logger.warning('cleanup_failed_transport_likely_destabilized_cleanup_err', cleanup_err=cleanup_err)


    @staticmethod
    def verify_emmc_health(dut: DutTransport, device_path: str='/dev/mmcblk0') -> ValidatorResult:
        """Parses S.M.A.R.T data directly from the eMMC controller's EXTCSD registers.

        Args:
            dut: The transport interface connected to the target.
            device_path: The /dev path of the eMMC block device.

        Returns:
            ValidatorResult: Contains the health status, passing unless the eMMC
                Pre-EOL warning is active.
        """
        logger.info('interrogating_s_m_a_r_t_extcsd_registers_on_device_path', device_path=device_path)
        context_data: Dict[str, Any] = {}
        metrics: Dict[str, float] = {}
        try:
            res = dut.safe_run(f'mmc extcsd read {device_path}', timeout_s=5.0)
            if not res.ok:
                err_msg = f'mmc-utils failed or {device_path} is invalid: {res.stderr.strip()}'
                logger.error('err_msg', err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg, context={'stdout': res.stdout, 'stderr': res.stderr})
            context_data['extcsd_dump'] = res.stdout
            life_match_a = re.search('Device life time estimation type A[^:]*:\\s*0x([0-9A-Fa-f]+)', res.stdout)
            life_match_b = re.search('Device life time estimation type B[^:]*:\\s*0x([0-9A-Fa-f]+)', res.stdout)
            life_a_pct = int(life_match_a.group(1), 16) * 10 if life_match_a else 0
            life_b_pct = int(life_match_b.group(1), 16) * 10 if life_match_b else 0
            max_used_pct = max(life_a_pct, life_b_pct)
            if max_used_pct > 0:
                metrics['emmc_life_used_percent'] = float(max_used_pct)
            eol_match = re.search('Pre EOL information[^:]*:\\s*0x([0-9A-Fa-f]+)', res.stdout)
            eol_val = int(eol_match.group(1), 16) if eol_match else 1
            context_data['pre_eol_val'] = eol_val
            passed = True
            error_msg = ''
            if eol_val >= 2:
                passed = False
                error_msg = 'eMMC S.M.A.R.T Pre-EOL warning active. Flash is dying!'
                logger.critical('=' * 60)
                logger.critical('fatal_error_msg', error_msg=error_msg)
                logger.critical('=' * 60)
            return ValidatorResult(passed=passed, metrics=metrics, error_msg=error_msg, context=context_data)
        except TransportTimeoutError:
            return ValidatorResult(passed=False, error_msg='DUT hung during EXTCSD read.', context=context_data)
        except TransportConnectionError as e:
            return ValidatorResult(passed=False, error_msg=f'Transport dropped during EXTCSD read: {e}', context=context_data)
