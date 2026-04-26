import structlog
import re
import time
import logging
import subprocess
from pathlib import Path
from typing import Optional
from pytest_mes_core.provisioning.base import BaseProvisioner, ProvisioningError
from pytest_mes_core.utils.process import LiveProcess, ProcessTimeoutError, ProcessExecutionError
from pytest_mes_core.transports.serial_client import EphemeralSerialClient
logger = structlog.get_logger('mes_core.provisioning.tezi')

class UuuTeziProvisioner(BaseProvisioner):
    """
    Zero-leakage manager for the NXP Universal Update Utility (uuu).
    Pushes TEZI images into SoC RAM via USB Serial Downloader mode.
    Enforces USB port isolation for parallel multi-jig environments.
    """
    _LSUSB_NXP_RE = re.compile('(1fc9|15a2):[0-9a-f]{4}', re.IGNORECASE)
    _UUU_RECOVERY_RE = re.compile('(SE Blank|SDP:)', re.IGNORECASE)
    _UUU_SUCCESS_RE = re.compile('(\\[\\s*Done|\\]\\s*Done\\b|Success\\s+[1-9]\\d*\\s+Failure\\s+0)', re.IGNORECASE)
    _UUU_FAIL_RE = re.compile('([\\[\\]]\\s*Fail\\b|uuu Failed)', re.IGNORECASE)
    _UUU_PERMISSION_RE = re.compile('(LIBUSB_ERROR_ACCESS|libusb_open failed|Access denied|Permission denied)', re.IGNORECASE)

    def __init__(self, wait_for_recovery_s: int=30, flash_timeout_s: int=300, usb_path: Optional[str]=None):
        self.wait_for_recovery_s = wait_for_recovery_s
        self.flash_timeout_s = flash_timeout_s
        self.usb_path = usb_path

    def _is_device_in_recovery(self) -> bool:
        """Polls the Linux USB tree to verify the SoC BootROM is visible.

        Returns:
            bool: True if the device is found in recovery mode.

        Raises:
            ProvisioningError: If the required 'lsusb' or 'uuu' tools are missing.
        """
        try:
            if self.usb_path:
                res_uuu = subprocess.run(['uuu', '-lsusb'], capture_output=True, text=True, timeout=5)
                out_uuu = res_uuu.stdout.lower() + res_uuu.stderr.lower()
                return self.usb_path in out_uuu
            res_lsusb = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=5)
            if self._LSUSB_NXP_RE.search(res_lsusb.stdout):
                return True
            res_uuu = subprocess.run(['uuu', '-lsusb'], capture_output=True, text=True, timeout=5)
            return bool(self._UUU_RECOVERY_RE.search(res_uuu.stdout))
        except subprocess.TimeoutExpired:
            return False
        except FileNotFoundError:
            err_msg = "FATAL: 'lsusb' or 'uuu' tool is not installed on Host PC."
            logger.critical('err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)

    def provision(self, image_path: Path, serial_client: Optional[EphemeralSerialClient]=None, success_prompt: str='login:') -> bool:
        """Pushes TEZI images into SoC RAM via USB Serial Downloader mode.

        Hardware Flow:
            1. Blocks until the DUT physical USB enumerates in NXP Recovery Mode.
            2. Executes 'uuu' targeting the specific USB port and TEZI folder.
            3. Uses LiveProcess to handle telemetry and artifact dumping.
            4. Optionally locks the UART and tails the OS boot until success_prompt is found.

        Args:
            image_path: The directory containing the TEZI payload and uuu.auto script.
            serial_client: Optional UART client to tail the live log for a success signature.
            success_prompt: The prompt to wait for before considering the boot successful.

        Returns:
            bool: True if the provisioning completes successfully.

        Raises:
            ProvisioningError: If the payload is invalid, the device doesn't enter recovery,
                the uuu command fails, or the flash times out.
        """
        tezi_dir = image_path
        if not tezi_dir.exists() or not tezi_dir.is_dir():
            err_msg = f'TEZI payload directory not found: {tezi_dir}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        if not (tezi_dir / 'uuu.auto').exists():
            err_msg = f'Invalid TEZI payload (missing uuu.auto inside {tezi_dir}).'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        target_str = f' on USB port {self.usb_path}' if self.usb_path else ''
        logger.info('waiting_up_to_wait_for_recovery_s_s_for_dut_to_enter_recovery_mode_target_str', wait_for_recovery_s=self.wait_for_recovery_s, target_str=target_str)
        t_wait_start = time.perf_counter()
        device_found = False
        while time.perf_counter() - t_wait_start < self.wait_for_recovery_s:
            logger.debug('[TEZI] Polling USB bus for NXP BootROM...')
            if self._is_device_in_recovery():
                device_found = True
                break
            time.sleep(0.5)
        if not device_found:
            err_msg = f'Timeout waiting for USB Recovery mode{target_str}. Is the boot jumper set?'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        logger.info('dut_detected_injecting_tezi_payload_from_name', name=tezi_dir.name)
        cmd = ['uuu']
        if self.usb_path:
            cmd.extend(['-m', self.usb_path])
        cmd.append(str(tezi_dir.absolute()))
        try:
            process = LiveProcess(cmd, self.flash_timeout_s, logger).execute()
            if process.returncode != 0:
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('tezi_fatal_uuu_rejected_the_payload_code_returncode_trace_saved_to_log_path', returncode=process.returncode, log_path=log_path)
                if self._UUU_PERMISSION_RE.search(process.stdout):
                    raise ProvisioningError("OS Permission Denied! You must either run pytest with 'sudo' or install the NXP udev rules so your user can access the USB device.")
                raise ProvisioningError(f'uuu lost USB sync (Code {process.returncode}).')
            if self._UUU_FAIL_RE.search(process.stdout) or not self._UUU_SUCCESS_RE.search(process.stdout):
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('tezi_fatal_uuu_falsely_exited_0_payload_never_executed_trace_saved_to_log_path', log_path=log_path)
                raise ProvisioningError("uuu script failed to execute fully. Missing 'Done' confirmation.")
            logger.info('tezi_flash_successfully_pushed_to_soc_ram_in_duration_s_s', duration_s=process.duration_s)
        except ProcessTimeoutError:
            raise ProvisioningError(f'uuu execution timed out after {self.flash_timeout_s}s! USB EMI reset?')
        except ProcessExecutionError as e:
            err_msg = f'Failed to execute uuu command: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        if serial_client:
            logger.info('[TEZI] Waiting for TEZI shell to start live log tailing...')
            if not serial_client.is_connected:
                serial_client.connect()
            
            serial_client.flush_buffers()
            t_end = time.perf_counter() + self.flash_timeout_s
            last_ping_time = time.perf_counter()
            tail_command_sent = False
            
            while time.perf_counter() < t_end:
                for line in serial_client.read_clean_stream(filter_kernel=False):
                    logger.debug('line', line=line)
                    last_ping_time = time.perf_counter()
                    if 'Successfully installed' in line or 'Rebooting' in line or (success_prompt and success_prompt in line):
                        logger.info('\n[TEZI] Installation Success Signature detected on completed line!')
                        return True
                
                if not tail_command_sent and any((p in serial_client.live_buffer for p in ['~ #', 'root@', '/ #'])):
                    logger.debug('val', val=serial_client.live_buffer.strip())
                    logger.info('\n[TEZI] TEZI Shell acquired! Injecting live log tracker...')
                    try:
                        serial_client.raw_write(b'tail -n +1 -f /var/volatile/tezi.log\n')
                        tail_command_sent = True
                        serial_client.parser.clear_buffer()
                    except Exception as e:
                        logger.warning('uart_write_blocked_e', e=e)
                    last_ping_time = time.perf_counter()
                    continue
                
                if 'Successfully installed' in serial_client.live_buffer or 'Rebooting' in serial_client.live_buffer or (success_prompt and success_prompt in serial_client.live_buffer):
                    logger.debug('val', val=serial_client.live_buffer.strip())
                    logger.info('\n[TEZI] Installation Success Signature detected in fragment!')
                    return True
                
                time.sleep(0.1)
                if not tail_command_sent and time.perf_counter() - last_ping_time > 3.0:
                    try:
                        serial_client.raw_write(b'\n')
                    except Exception:
                        pass
                    last_ping_time = time.perf_counter()
            
            logger.critical('tezi_fatal_failed_to_complete_installation_within_flash_timeout_s_s', flash_timeout_s=self.flash_timeout_s)
            return False
        return True