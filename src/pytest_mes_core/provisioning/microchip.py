import anyio
import structlog
import logging
from pathlib import Path
from typing import List, Optional
from pytest_mes_core.provisioning.base import BaseProvisioner, ProvisioningError, ImageVerificationError, SiliconLockError
from pytest_mes_core.host_adapters.microchip import HostPickitAdapter
from pytest_mes_core.utils.process import LiveProcess, ProcessTimeoutError, ProcessExecutionError
logger = structlog.get_logger('mes_core.provisioning.microchip')

class MicrochipIpeProvisioner(BaseProvisioner):
    """
    Executes Microchip's IPECMD tool over a securely locked Host Adapter.
    Validates output defensively against JVM quirks and ensures deep observability.
    """

    def __init__(self, pickit_adapter: HostPickitAdapter, ipecmd_path: str, device: str, timeout_s: int=45, extra_flags: Optional[List[str]]=None):
        self.pickit = pickit_adapter
        self.ipecmd_path = Path(ipecmd_path)
        self.device = device
        self.timeout_s = timeout_s
        self.extra_flags = extra_flags or []

    def provision(self, image_path: Path) -> bool:
        """Flashes the target using Microchip's IPECMD tool over a securely locked Host Adapter.

        Args:
            image_path: The path to the hex firmware image.

        Returns:
            bool: True if provisioning is successful.

        Raises:
            ProvisioningError: If the executable or firmware is missing, target VDD is missing,
                JVM crashes, target silicon is not detected, or confirmation is missing.
            ImageVerificationError: If readback verification fails.
            SiliconLockError: If the PIC Configuration Bits are locked.
        """
        if not self.ipecmd_path.exists():
            err_msg = f'IPECMD executable not found at {self.ipecmd_path}.'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        if not image_path.exists():
            err_msg = f'Firmware hex not found: {image_path}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        logger.info('initiating_ipecmd_flash_of_device_with_name', device=self.device, name=image_path.name)
        cmd = []
        if self.ipecmd_path.suffix == '.jar':
            cmd.extend(['java', '-jar', str(self.ipecmd_path)])
        else:
            cmd.append(str(self.ipecmd_path))
        cmd.extend([f'-P{self.device}', f'-TS{self.pickit.tool_serial}', f'-F{image_path.resolve()}', '-M', '-Y'])
        if self.extra_flags:
            cmd.extend(self.extra_flags)
        try:
            process = LiveProcess(cmd, self.timeout_s, logger).execute()
            stdout_lower = process.stdout.lower()
            if 'no voltage has been detected on vdd' in stdout_lower:
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('icsp_fatal_target_is_unpowered_add_w3_3_to_extra_flags_or_power_the_board_trace_log_path', log_path=log_path)
                raise ProvisioningError('Target VDD missing. ICSP refused to connect.')
            if 'exception in thread' in stdout_lower or 'outofmemoryerror' in stdout_lower:
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('icsp_fatal_java_virtual_machine_crashed_trace_saved_to_log_path', log_path=log_path)
                raise ProvisioningError('IPECMD JVM crashed. Check Host PC RAM and Java version.')
            if 'target device was not found' in stdout_lower:
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('icsp_fatal_pickit_could_not_detect_the_device_pogo_pin_failure_trace_saved_to_log_path', device=self.device, log_path=log_path)
                raise ProvisioningError('Target silicon not detected. Check physical ICSP connections and VDD.')
            if 'verify failed' in stdout_lower:
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('icsp_fatal_flash_verify_failed_bad_sector_or_noisy_clock_line_trace_log_path', log_path=log_path)
                raise ImageVerificationError('ICSP readback verification failed.')
            if 'protected' in stdout_lower or 'code protect' in stdout_lower:
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('icsp_fatal_silicon_is_read_write_protected_trace_saved_to_log_path', log_path=log_path)
                raise SiliconLockError('PIC Configuration Bits are locked. Cannot flash.')
            if 'program succeeded' not in stdout_lower and 'programming complete' not in stdout_lower:
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('icsp_fatal_missing_positive_confirmation_from_ipecmd_trace_saved_to_log_path', log_path=log_path)
                raise ProvisioningError('IPECMD returned exit code 0, but did not confirm programming was complete.')
            logger.info('icsp_successfully_flashed_and_verified_device_in_duration_s_s', device=self.device, duration_s=process.duration_s)
            return True
        except ProcessTimeoutError:
            raise ProvisioningError(f'PIC flash operation timed out after {self.timeout_s}s. JVM Deadlock.')
        except ProcessExecutionError as e:
            err_msg = f'Failed to execute IPECMD script. Error: {e}'
            logger.critical('icsp_fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
    async def async_provision(self, image_path, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.provision, image_path, *args, **kwargs)
