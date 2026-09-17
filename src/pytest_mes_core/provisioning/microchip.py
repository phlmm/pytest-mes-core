import time
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

    def __init__(
        self,
        pickit_adapter: HostPickitAdapter,
        ipecmd_path: str,
        device: str,
        timeout_s: int = 60,
        extra_flags: Optional[List[str]] = None,
        retries: int = 1,
    ):
        self.pickit = pickit_adapter
        self.ipecmd_path = Path(ipecmd_path)
        self.device = device
        self.timeout_s = timeout_s
        self.extra_flags = extra_flags or []
        self.retries = retries

    def _execute_flash(self, cmd: List[str], execution_cwd: Optional[Path]) -> bool:
        """Executes a single invocation of IPECMD and analyzes the outcome."""
        try:
            process = LiveProcess(cmd, self.timeout_s, logger, cwd=execution_cwd).execute()
            stdout_lower = process.stdout.lower()
            artifact_dir = Path('/tmp/mes_artifacts')

            if 'no voltage has been detected on vdd' in stdout_lower:
                log_path = process.export_log(artifact_dir)
                logger.critical('icsp_fatal_target_is_unpowered_add_w3_3_to_extra_flags_or_power_the_board_trace_log_path', log_path=log_path)
                raise ProvisioningError('Target VDD missing. ICSP refused to connect. Ensure target is energized or add -W3.3 to extra_flags.')

            if 'target device was not found' in stdout_lower or 'target silicon not detected' in stdout_lower:
                log_path = process.export_log(artifact_dir)
                logger.critical('icsp_fatal_pickit_could_not_detect_the_device_pogo_pin_failure_trace_saved_to_log_path', device=self.device, log_path=log_path)
                raise ProvisioningError(f"Target silicon '{self.device}' not detected. Check physical ICSP connections and VDD.")

            if 'verify failed' in stdout_lower:
                log_path = process.export_log(artifact_dir)
                logger.critical('icsp_fatal_flash_verify_failed_bad_sector_or_noisy_clock_line_trace_log_path', log_path=log_path)
                raise ImageVerificationError('ICSP readback verification failed.')

            if 'protected' in stdout_lower or 'code protect' in stdout_lower:
                log_path = process.export_log(artifact_dir)
                logger.critical('icsp_fatal_silicon_is_read_write_protected_trace_saved_to_log_path', log_path=log_path)
                raise SiliconLockError('PIC Configuration Bits are locked. Cannot flash.')

            if 'could not find a tool with the serial number' in stdout_lower or 'no tool found' in stdout_lower:
                log_path = process.export_log(artifact_dir)
                logger.critical('icsp_fatal_tool_not_found', serial=self.pickit.tool_serial, log_path=log_path)
                raise ProvisioningError(f"Microchip probe with serial '{self.pickit.tool_serial}' not found by IPECMD.")

            if 'connection failed' in stdout_lower or 'failed to connect' in stdout_lower:
                log_path = process.export_log(artifact_dir)
                logger.critical('icsp_fatal_connection_failed', log_path=log_path)
                raise ProvisioningError('IPECMD connection failed. Target could not be contacted.')

            if 'exception in thread' in stdout_lower or 'outofmemoryerror' in stdout_lower:
                log_path = process.export_log(artifact_dir)
                logger.critical('icsp_fatal_java_virtual_machine_crashed_trace_saved_to_log_path', log_path=log_path)
                raise ProvisioningError('IPECMD JVM crashed. Check Host PC RAM and Java version.')

            if process.returncode != 0:
                log_path = process.export_log(artifact_dir)
                summary_lines = ' | '.join([l.strip() for l in process.stdout.strip().splitlines()[-3:] if l.strip()])
                logger.critical('icsp_fatal_nonzero_exit_code', returncode=process.returncode, log_path=log_path)
                raise ProvisioningError(f'IPECMD returned non-zero exit code {process.returncode}: {summary_lines}')

            if 'program succeeded' not in stdout_lower and 'programming complete' not in stdout_lower:
                log_path = process.export_log(artifact_dir)
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
        
        # Sanitize extra_flags if tool_type is known
        tool_type = getattr(self.pickit, 'tool_type', 'UNKNOWN')
        sanitized_flags = []
        for flag in self.extra_flags:
            if flag.upper().startswith('-TP') and tool_type != 'UNKNOWN':
                expected_tp = f'-TP{tool_type}'
                if flag.upper() != expected_tp:
                    logger.warning(
                        'correcting_mismatched_tool_type_flag',
                        configured_flag=flag,
                        corrected_flag=expected_tp,
                        detected_tool=tool_type
                    )
                    sanitized_flags.append(expected_tp)
                    continue
            if flag.upper() == '-OH':
                # In IPECMD, -OH means unselect 'Erase All Before Programming'.
                # Omitting -OH ensures full chip erase before programming and verification.
                logger.warning(
                    'omitting_flag_oh_which_disables_erase_before_programming',
                    flag=flag
                )
                continue
            sanitized_flags.append(flag)

        cmd = []
        if self.ipecmd_path.suffix == '.jar':
            cmd.extend(['java', '-jar', str(self.ipecmd_path)])
        else:
            cmd.append(str(self.ipecmd_path.resolve()))

        # Core target identification and payload
        cmd.extend([f'-P{self.device}', f'-TS{self.pickit.tool_serial}', f'-F{image_path.resolve()}'])

        # Guarantee bulk erase (-E), program (-M), and verify (-Y) are included
        if not any(f.upper() == '-E' for f in sanitized_flags):
            cmd.append('-E')
        if not any(f.upper().startswith('-M') for f in sanitized_flags):
            cmd.append('-M')
        if not any(f.upper().startswith('-Y') for f in sanitized_flags):
            cmd.append('-Y')

        if sanitized_flags:
            cmd.extend(sanitized_flags)

        # Microchip ipecmd scripts require executing in their directory to resolve etc/mplab_ipe.conf
        execution_cwd = self.ipecmd_path.parent if self.ipecmd_path.is_file() else None

        total_attempts = 1 + max(0, self.retries)
        for attempt in range(1, total_attempts + 1):
            try:
                return self._execute_flash(cmd, execution_cwd)
            except (ImageVerificationError, ProvisioningError) as e:
                if isinstance(e, SiliconLockError):
                    raise
                if attempt < total_attempts:
                    logger.warning(
                        'icsp_flash_attempt_failed_retrying',
                        attempt=attempt,
                        total_attempts=total_attempts,
                        error=str(e),
                        backoff_s=2.0
                    )
                    time.sleep(2.0)
                else:
                    raise
