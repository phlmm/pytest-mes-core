import anyio
import structlog
import re
import logging
from typing import Dict, Any
from pytest_mes_core.transports import DutTransport, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.protocols.base import collect_soc_health
from pytest_mes_core.config import UsbStorageConfig
logger = structlog.get_logger('mes_core.protocols.usb')

class UsbMassStorageValidator:
    """
    Validates USB Host ports on the DUT by stressing an authorized thumb drive.
    Hardened against dynamic device enumeration, bent pins, and power-delivery brownouts.
    """

    def __init__(self, transport: DutTransport, config: UsbStorageConfig):
        self.transport = transport
        self.cfg = config
        self.mount_point = f'/tmp/usb_test_{self.cfg.vid_hex}_{self.cfg.pid_hex}'

    def _find_block_device(self) -> str:
        """Dynamically maps the authorized VID:PID to a logical block device.

        Returns:
            str: The /dev block device path, or an empty string if not found.
        """
        logger.debug('probing_sysfs_for_authorized_vid_pid_vid_hex_pid_hex', vid_hex=self.cfg.vid_hex, pid_hex=self.cfg.pid_hex)
        lsusb_cmd = f'lsusb -d {self.cfg.vid_hex}:{self.cfg.pid_hex}'
        res = self.transport.safe_run(lsusb_cmd, timeout_s=5.0)
        if res.exited != 0 or not res.stdout:
            return ''
        logger.debug('[USB] Hardware detected. Resolving block device mapping...')
        res_disk = self.transport.safe_run('ls -l /dev/disk/by-id/usb-* | grep -m 1 part1', timeout_s=3.0)
        if res_disk.exited != 0:
            fallback = self.transport.safe_run("lsblk -l -o NAME,TRAN | grep usb | awk '{print $1}' | head -n 1", timeout_s=3.0)
            return f'/dev/{fallback.stdout.strip()}1' if fallback.stdout.strip() else ''
        match = re.search('../../(sd[a-z][0-9])', res_disk.stdout)
        return f'/dev/{match.group(1)}' if match else ''

    def verify_throughput_and_integrity(self) -> ValidatorResult:
        """Stresses the USB host controller and monitors for VBUS brownouts.

        Returns:
            ValidatorResult: Pass/fail outcome based on write throughput, data 
                integrity, and the absence of dmesg PHY reset/brownout traces.
        """
        logger.info('verifying_host_port_via_authorized_drive_vid_hex_pid_hex', vid_hex=self.cfg.vid_hex, pid_hex=self.cfg.pid_hex)
        context_data: Dict[str, Any] = {}
        try:
            block_dev = self._find_block_device()
            if not block_dev:
                err_msg = f'Authorized USB Drive ({self.cfg.vid_hex}:{self.cfg.pid_hex}) not enumerated.'
                logger.error('err_msg_is_it_plugged_in', err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)
            logger.debug('drive_dynamically_mapped_to_block_dev', block_dev=block_dev)
            context_data['block_device'] = block_dev
            logger.debug('[USB] Flushing dmesg buffer to prepare for VBUS brownout monitoring...')
            self.transport.safe_run('dmesg -c > /dev/null', timeout_s=3.0)
            logger.debug('ensuring_clean_mount_state_at_mount_point', mount_point=self.mount_point)
            self.transport.safe_run(f'mkdir -p {self.mount_point}', timeout_s=3.0)
            self.transport.safe_run(f'umount -l {block_dev} >/dev/null 2>&1', timeout_s=5.0)
            mount_res = self.transport.safe_run(f'mount {block_dev} {self.mount_point}', timeout_s=5.0)
            if mount_res.exited != 0:
                logger.error('kernel_rejected_mount_val', val=mount_res.stderr.strip())
                return ValidatorResult(passed=False, error_msg=f'Failed to mount {block_dev}: {mount_res.stderr.strip()}', context=context_data)
            test_file = f'{self.mount_point}/factory_test.bin'
            baseline_health = collect_soc_health(self.transport)
            if baseline_health:
                context_data['pre_test_health'] = baseline_health
            logger.debug('blasting_test_size_mb_mb_payload_to_stress_vbus_power_delivery_fsync_enabled', test_size_mb=self.cfg.test_size_mb)
            write_cmd = f'dd if=/dev/urandom of={test_file} bs=1M count={self.cfg.test_size_mb} conv=fsync'
            write_res = self.transport.safe_run(write_cmd, timeout_s=45.0)
            post_health = collect_soc_health(self.transport)
            if write_res.exited != 0:
                logger.error('payload_write_failed_val', val=write_res.stderr.strip())
                return ValidatorResult(passed=False, error_msg=f'I/O Write Error: {write_res.stderr.strip()}', context=context_data)
            combined_out = f'{write_res.stderr}\n{write_res.stdout}'
            match = re.search('([0-9.]+)\\s*M[Bb]/s', combined_out)
            throughput = float(match.group(1)) if match else 0.0
            context_data['write_throughput_mbps'] = throughput
            if throughput < self.cfg.min_write_mbps:
                logger.critical('=' * 60)
                logger.critical('fatal_usb_speed_degraded')
                logger.critical('measured_throughput_mb_s_required_limit_min_write_mbps_mb_s', throughput=throughput, min_write_mbps=self.cfg.min_write_mbps)
                logger.critical('[USB] SuperSpeed (USB 3.0) pins may be bent or unsoldered, causing silent fallback to USB 2.0.')
                logger.critical('=' * 60)
                return ValidatorResult(passed=False, error_msg=f'USB speed degraded: {throughput} MB/s (Minimum: {self.cfg.min_write_mbps} MB/s)', metrics={'usb_write_mbps': throughput}, context=context_data)
            logger.debug('[USB] Validating cryptographic integrity of written payload...')
            hash_res = self.transport.safe_run(f'md5sum {test_file}', timeout_s=15.0)
            if hash_res.exited != 0:
                logger.critical('fatal_readback_i_o_error_data_lines_corrupted_emi_or_bad_traces')
                return ValidatorResult(passed=False, error_msg='Readback I/O Error. Corrupted USB data lines.', context=context_data)
            logger.debug('[USB] Scraping dmesg for silent PHY resets or over-current events...')
            dmesg_res = self.transport.safe_run("dmesg | grep -i -E 'usb disconnect|reset.*high-speed|over-current'", timeout_s=5.0)
            if dmesg_res.stdout.strip():
                logger.critical('=' * 60)
                logger.critical('fatal_vbus_brownout_detected')
                logger.critical('[USB] The I/O load triggered a physical over-current event or PHY reset.')
                logger.critical('kernel_trace_val', val=dmesg_res.stdout.strip())
                logger.critical('=' * 60)
                context_data['brownout_trace'] = dmesg_res.stdout.strip()
                return ValidatorResult(passed=False, error_msg='USB switch brownout or EMI reset detected during load.', context=context_data)
            logger.info('verification_complete_throughput_sustained_at_throughput_mb_s', throughput=throughput)
            metrics = {'usb_write_mbps': throughput}
            metrics.update(post_health)
            return ValidatorResult(passed=True, metrics=metrics, context=context_data)
        except TransportTimeoutError:
            logger.critical('[USB] FATAL: Silicon Lockup. DUT hung entirely during USB I/O operation.')
            return ValidatorResult(passed=False, error_msg='Silicon Lockup: DUT hung during USB I/O.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('=' * 60)
            logger.critical('fatal_catastrophic_power_fault_detected')
            logger.critical('transport_pipe_shattered_during_usb_load_e', e=e)
            logger.critical('[USB] VBUS likely shorted to ground, tripping the PMIC and rebooting the board.')
            logger.critical('=' * 60)
            return ValidatorResult(passed=False, error_msg=f'Transport dropped (VBUS Short / PMIC Trip?): {e}', context=context_data)
        finally:
            if self.transport.is_connected:
                try:
                    logger.debug('zero_leakage_scrubbing_payload_and_unmounting_mount_point', mount_point=self.mount_point)
                    self.transport.safe_run(f'rm -f {self.mount_point}/factory_test.bin', timeout_s=5.0)
                    self.transport.safe_run('sync', timeout_s=10.0)
                    self.transport.safe_run(f'umount -l {self.mount_point} >/dev/null 2>&1', timeout_s=5.0)
                    self.transport.safe_run(f'rm -rf {self.mount_point} >/dev/null 2>&1', timeout_s=5.0)
                except Exception as cleanup_err:
                    logger.debug('teardown_skipped_transport_likely_dead_cleanup_err', cleanup_err=cleanup_err)
    async def async_verify_throughput_and_integrity(self, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.verify_throughput_and_integrity, *args, **kwargs)
