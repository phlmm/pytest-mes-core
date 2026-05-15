import structlog
import re
import time
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional
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

    def provision(self, image_path: Path, serial_client: Optional[EphemeralSerialClient] = None,
                  success_prompt: str = 'login:', fsm: Optional[Any] = None) -> bool:
        """
        Pushes TEZI images into SoC RAM via USB Serial Downloader mode.

        Hardware Flow:
            1. Blocks until the DUT physical USB enumerates in NXP Recovery Mode.
            2. Executes 'uuu' targeting the specific USB port and TEZI folder.
            3. Uses LiveProcess to handle telemetry and artifact dumping.
            4. Calls fsm.release_recovery() so the FSM/strategy decides whether to
               auto-release a GPIO pin or prompt the operator to remove a jumper.
            5. Optionally locks the UART and tails the OS boot until success_prompt is found.

        Args:
            image_path:     The directory containing the TEZI payload and uuu.auto script.
            serial_client:  Optional UART client to tail the live log for a success signature.
            success_prompt: The prompt to wait for before considering the boot successful.
            fsm:            Optional FSM instance.  When supplied, release_recovery() is called
                            after payload delivery so the configured RecoveryStrategy can
                            release the strap (GPIO no-op or manual-jumper prompt).

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
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                raise ProvisioningError("USB polling interrupted by operator (Ctrl+C).")
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
            # Delegate strap-release to the FSM's RecoveryStrategy.  GPIO-automated
            # stations are a no-op; manual-jumper stations show the operator prompt.
            if fsm is not None:
                fsm.release_recovery()
        except ProcessTimeoutError:
            raise ProvisioningError(f'uuu execution timed out after {self.flash_timeout_s}s! USB EMI reset?')
        except ProcessExecutionError as e:
            err_msg = f'Failed to execute uuu command: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        if serial_client:
            logger.info('[TEZI] Waiting for TEZI OS shell to begin live log tailing...')
            if not serial_client.is_connected:
                serial_client.connect()

            # Subscribe via the pub/sub multiplexer so UartKernelWatchdog continues
            # to receive every byte concurrently (zero data loss, no race window).
            from pytest_mes_core.state_machine import UartEventStream
            from pytest_mes_core.events import PromptDetected, PanicDetected
            from pytest_mes_core.transports.constants import ANSI_ESCAPE_B, PANIC_PATTERN_B
            import re

            stream = UartEventStream(
                serial=serial_client,
                ansi_pattern=ANSI_ESCAPE_B,
                panic_pattern=PANIC_PATTERN_B,
            )
            prompts = {
                # TEZI shell acquired — inject the live log tail command
                "tezi_shell_hash": b"~ #",
                "tezi_shell_root": b"root@",
                "tezi_shell_slash": b"/ #",
                # Success signatures (written by TEZI itself)
                "success_installed": b"Successfully installed",
                "success_rebooting": b"Rebooting",
                # Optional custom prompt (e.g. "login:")
                "success_prompt": success_prompt.encode() if success_prompt else b"login:",
            }
            tail_sent = False
            # Do NOT flush here.  The serial port was already flushed at connect()
            # time, before uuu ran.  Every byte arriving since then is live TEZI
            # boot output that we need to see.  Flushing at this point would erase
            # the TEZI shell prompt that the board already printed in the window
            # between release_recovery() and this subscribe call — which is exactly
            # the stale-buffer bug that caused the 66-second timeout failure.
            # Instead we send a single \n ping immediately so the shell re-draws its
            # prompt in case we just missed it.
            try:
                serial_client.raw_write(b'\n')
            except Exception:
                pass
            for event in stream.open(
                prompts=prompts,
                timeout_s=self.flash_timeout_s,
                flush=False,
                active_ping_char=b'\n',
            ):
                if isinstance(event, PanicDetected):
                    logger.critical('[TEZI] Kernel panic detected during TEZI install! Aborting.')
                    return False

                if isinstance(event, PromptDetected):
                    if event.prompt_type in ("tezi_shell_hash", "tezi_shell_root", "tezi_shell_slash"):
                        if not tail_sent:
                            logger.info('[TEZI] TEZI Shell acquired! Injecting live log tracker...')
                            try:
                                serial_client.raw_write(b'tail -n +1 -f /var/volatile/tezi.log\n')
                                serial_client.parser.clear_buffer()
                                tail_sent = True
                            except Exception as e:
                                logger.warning('uart_write_blocked_e', e=e)
                    elif event.prompt_type in ("success_installed", "success_rebooting", "success_prompt"):
                        logger.info('[TEZI] Installation Success Signature detected! TEZI flash complete.')
                        if fsm is not None:
                            # The TEZI installer has finished and the board is rebooting
                            # into the newly-flashed eMMC.  The framework has no further
                            # control over the autonomous TEZI process — this is the
                            # correct point to exit RECOVERY state.
                            from pytest_mes_core.state_machine import DutState
                            fsm.state = DutState.ENERGIZED
                            logger.info('fsm_state_advanced_recovery_to_energized')
                        return True

            logger.critical(
                'tezi_fatal_failed_to_complete_installation_within_flash_timeout_s_s',
                flash_timeout_s=self.flash_timeout_s,
            )
            return False

        # No serial_client supplied — uuu exit code 0 is sufficient proof of success.
        return True