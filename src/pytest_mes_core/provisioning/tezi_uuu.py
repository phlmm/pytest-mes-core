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

    @staticmethod
    def _invoke_release_recovery(fsm: Any) -> None:
        """Bridge to the FSM's async release_recovery from this sync context.

        provision() may run inside an anyio worker thread (async_provision path)
        or in a plain sync context; handle both. Sync callables (test mocks,
        custom FSMs) are invoked directly.
        """
        import inspect
        import anyio
        fn = fsm.release_recovery
        if inspect.iscoroutinefunction(fn):
            try:
                anyio.from_thread.run(fn)      # inside an anyio worker thread
            except RuntimeError:
                anyio.run(fn)                  # plain sync caller, no loop
        else:
            fn()

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
            
            # 1. Native OS Radar (Bypasses uuu permission/sudo traps)
            res_lsusb = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=5)
            if self._LSUSB_NXP_RE.search(res_lsusb.stdout):
                return True
                
            # 2. Fallback to uuu (Check stderr as well, where uuu sometimes prints)
            res_uuu = subprocess.run(['uuu', '-lsusb'], capture_output=True, text=True, timeout=5)
            return bool(self._UUU_RECOVERY_RE.search(res_uuu.stdout))
        except subprocess.TimeoutExpired:
            return False
        except FileNotFoundError:
            err_msg = "FATAL: 'lsusb' or 'uuu' tool is not installed on Host PC."
            logger.critical('err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)

    async def async_provision(self, image_path: Path, serial_client: Optional['EphemeralSerialClient'] = None, success_prompt: str = "login:", fsm: Optional[Any] = None) -> bool:
        import anyio
        return await anyio.to_thread.run_sync(self.provision, image_path, serial_client, success_prompt, fsm)

    def provision(
        self,
        image_path: Path,
        serial_client: Optional['EphemeralSerialClient'] = None,
        success_prompt: str = "login:",
        fsm: Optional[Any] = None,
    ) -> bool:
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
                time.sleep(0.5) # Fast poll to snap execution instantly
            except KeyboardInterrupt:
                raise ProvisioningError("USB polling interrupted by operator (Ctrl+C).")
        if not device_found:
            err_msg = f'Timeout waiting for USB Recovery mode{target_str}. Is the boot jumper set?'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
            
        logger.info('dut_detected_injecting_tezi_payload_from_name', name=tezi_dir.name)
        
        # 2. Execute uuu securely
        # Note: If the host lacks NXP udev rules, uuu will fail here with a libusb permission error.
        # The operator must either run Pytest with sudo, or install the udev rules.
        cmd = ['uuu']
        if self.usb_path:
            cmd.extend(['-m', self.usb_path])
        cmd.append(str(tezi_dir.absolute()))
        try:
            process = LiveProcess(cmd, self.flash_timeout_s, logger).execute()
            # Strip ANSI escape sequences from stdout to handle colored terminal text
            clean_stdout = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', process.stdout)
            
            # 3. Analyze output physics
            if process.returncode != 0:
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('tezi_fatal_uuu_rejected_the_payload_code_returncode_trace_saved_to_log_path', returncode=process.returncode, log_path=log_path)
                if self._UUU_PERMISSION_RE.search(clean_stdout):
                    raise ProvisioningError("OS Permission Denied! You must either run pytest with 'sudo' or install the NXP udev rules so your user can access the USB device.")
                raise ProvisioningError(f'uuu lost USB sync (Code {process.returncode}).')
            if self._UUU_FAIL_RE.search(clean_stdout) or not self._UUU_SUCCESS_RE.search(clean_stdout):
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.critical('tezi_fatal_uuu_falsely_exited_0_payload_never_executed_trace_saved_to_log_path', log_path=log_path)
                raise ProvisioningError("uuu script failed to execute fully. Missing 'Done' confirmation.")
            
            logger.info('tezi_flash_successfully_pushed_to_soc_ram_in_duration_s_s', duration_s=process.duration_s)
            
            # Delegate strap-release to the FSM's RecoveryStrategy.  GPIO-automated
            # stations are a no-op; manual-jumper stations show the operator prompt.
            if fsm is not None:
                self._invoke_release_recovery(fsm)
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

            try:
                # Subscribe via the pub/sub multiplexer so UartKernelWatchdog continues
                # to receive every byte concurrently (zero data loss, no race window).
                from pytest_mes_core.state_machine import UartEventStream
                from pytest_mes_core.events import PromptDetected, PanicDetected, BootDataReceived
                from pytest_mes_core.transports.constants import ANSI_ESCAPE_B, PANIC_PATTERN_B

                stream = UartEventStream(
                    serial=serial_client,
                    ansi_pattern=ANSI_ESCAPE_B,
                    panic_pattern=PANIC_PATTERN_B,
                )
                _TX_PROBE_TOKEN = '__MES_TX_OK__'
                _MEDIA_CHECK_TOKEN_OK = '__MES_MEDIA_OK__'
                _MEDIA_CHECK_TOKEN_MISSING = '__MES_MEDIA_MISSING__'

                # Split the tokens to prevent the shell script from echo'ing them contiguously on UART
                split_ok = len(_MEDIA_CHECK_TOKEN_OK) // 2
                ok_part1 = _MEDIA_CHECK_TOKEN_OK[:split_ok]
                ok_part2 = _MEDIA_CHECK_TOKEN_OK[split_ok:]
                shell_ok = f'"{ok_part1}""{ok_part2}"'.encode()

                split_missing = len(_MEDIA_CHECK_TOKEN_MISSING) // 2
                missing_part1 = _MEDIA_CHECK_TOKEN_MISSING[:split_missing]
                missing_part2 = _MEDIA_CHECK_TOKEN_MISSING[split_missing:]
                shell_missing = f'"{missing_part1}""{missing_part2}"'.encode()
                prompts = {
                    # TEZI shell acquired — inject the TX health probe
                    "tezi_shell_hash": b"~ #",
                    "tezi_shell_root": b"root@",
                    "tezi_shell_slash": b"/ #",
                    # Success signatures (written by TEZI itself / tezi.log)
                    "success_installed": b"Successfully installed",
                    "success_rebooting": b"Restarting system",
                    # Optional custom prompt from tezi.log (e.g. "Flushing buffers...")
                    "success_prompt": success_prompt.encode() if success_prompt else b"login:",
                    # Fallback: if the board reboots from TEZI and reaches the
                    # eMMC OS login prompt, that is conclusive proof the flash
                    # succeeded — even when tezi.log was never tailed.
                    "post_install_login": b"login:",
                }
                tail_sent = False
                _tx_probe_sent = False
                _tx_verified = False
                _tx_probe_deadline = 0.0
                _media_check_sent = False
                _media_check_verified = False
                _media_check_deadline = 0.0
                _shell_fallback_deadline = time.perf_counter() + 15.0

                def _report_flash_success() -> bool:
                    """Advances the FSM (if any) and logs the shared success body."""
                    logger.info('[TEZI] Installation Success Signature detected! TEZI flash complete.')
                    if fsm is not None:
                        from pytest_mes_core.state_machine import DutState
                        fsm.machine.set_state(DutState.ENERGIZED)
                        logger.info('fsm_state_advanced_recovery_to_energized')
                    return True

                # To clear any stale data (including data buffered in the USB-to-serial chip/OS
                # which doesn't show up until we send \r\n), we perform an interactive hardware flush.
                try:
                    serial_client.raw_write(b'\r\n')
                    time.sleep(0.1)
                except Exception:
                    pass
                for event in stream.open(
                    prompts=prompts,
                    timeout_s=self.flash_timeout_s,
                    flush=True,
                    active_ping_char=b'\n',
                ):
                    if isinstance(event, PanicDetected):
                        logger.critical('[TEZI] Kernel panic detected during TEZI install! Aborting.')
                        return False

                    # Check deadlines on every event loop iteration
                    if _tx_probe_sent and not _tx_verified and time.perf_counter() > _tx_probe_deadline:
                        logger.error(
                            '[TEZI] TX health check FAILED: echo probe not returned after 10 s. '
                            'Host→DUT UART TX line may be broken or disconnected. '
                            'Continuing in RX-only mode — relying on login: prompt fallback.'
                        )
                        _tx_verified = True  # stop checking
                        tail_sent = True     # skip tail, rely on post_install_login

                    if _media_check_sent and not _media_check_verified and time.perf_counter() > _media_check_deadline:
                        logger.critical('[TEZI] Removable media check timed out: no response from DUT shell!')
                        raise ProvisioningError("Timeout waiting for removable media check response from DUT!")

                    if not tail_sent and not _tx_probe_sent and time.perf_counter() > _shell_fallback_deadline:
                        logger.warning(
                            '[TEZI] TEZI shell not detected within 15 s. '
                            'Proceeding to RX-only mode — relying on login: prompt fallback.'
                        )
                        tail_sent = True

                    if isinstance(event, PromptDetected):
                        if event.prompt_type in ("tezi_shell_hash", "tezi_shell_root", "tezi_shell_slash"):
                            if not tail_sent and not _tx_probe_sent:
                                # Step 1: Verify TX link before sending commands.
                                # A broken host→DUT wire silently swallows everything.
                                logger.info('[TEZI] TEZI Shell detected. Probing TX health...')
                                try:
                                    serial_client.raw_write(f'echo {_TX_PROBE_TOKEN}\n'.encode())
                                    _tx_probe_sent = True
                                    _tx_probe_deadline = time.perf_counter() + 10.0
                                except Exception as e:
                                    logger.error('[TEZI] TX probe write failed', error=str(e))
                                    tail_sent = True  # skip tail, rely on RX-only fallback
                        elif event.prompt_type in ("success_installed", "success_rebooting", "post_install_login"):
                            # These three signatures are conclusive proof of a successful
                            # flash whenever they appear — they cannot occur pre-install
                            # (the stream was flushed at open()) — so they are trusted
                            # even if seen before tail_sent is set.
                            return _report_flash_success()
                        elif event.prompt_type == "success_prompt":
                            if not tail_sent:
                                # Generic caller-supplied pattern (default "login:") — only
                                # trusted once we're actively tailing tezi.log, otherwise a
                                # stray pre-install login prompt could be mistaken for success.
                                continue
                            return _report_flash_success()

                    # Step 2: Watch for the TX probe echo and media check in data events.
                    if isinstance(event, BootDataReceived):
                        if _tx_probe_sent and not _tx_verified:
                            if _TX_PROBE_TOKEN in event.line:
                                _tx_verified = True
                                logger.info('[TEZI] TX health verified! Probing removable media presence...')
                                try:
                                    # Chunking write to prevent UART FIFO overflow on target
                                    script_chunks = [
                                        b'attempt=1; media_found=0; ',
                                        b'while [ $attempt -le 10 ]; do ',
                                        b'emmc_base=""; ',
                                        b'for b in /sys/block/*boot0; do ',
                                        b'if [ -e "$b" ]; then ',
                                        b'b_name="${b##*/}"; emmc_base="${b_name%boot0}"; break; ',
                                        b'fi; done; ',
                                        b'for d in /sys/block/sd* /sys/block/mmcblk*; do ',
                                        b'if [ -d "$d" ]; then ',
                                        b'd_name="${d##*/}"; ',
                                        b'if [ -n "$emmc_base" ]; then ',
                                        b'case "$d_name" in "$emmc_base"*) continue;; esac; ',
                                        b'fi; ',
                                        b'if [ -f "$d/removable" ] && [ "$(cat $d/removable)" = "1" ]; then ',
                                        b'media_found=1; break; fi; ',
                                        b'case "$d_name" in mmcblk*) ',
                                        b'if [ -f "$d/size" ] && [ "$(cat $d/size)" -gt 0 ]; then ',
                                        b'media_found=1; break; fi;; esac; ',
                                        b'fi; done; ',
                                        b'if [ "$media_found" = "1" ]; then break; fi; ',
                                        b'attempt=$((attempt+1)); sleep 1; done; ',
                                        b'if [ "$media_found" = "1" ]; then echo ' + shell_ok + b'; ',
                                        b'else echo ' + shell_missing + b'; fi\n'
                                    ]
                                    for chunk in script_chunks:
                                        serial_client.raw_write(chunk)
                                        time.sleep(0.01)
                                    _media_check_sent = True
                                    _media_check_deadline = time.perf_counter() + 15.0
                                except Exception as e:
                                    logger.error('[TEZI] Removable media probe write failed', error=str(e))
                                    tail_sent = True  # skip tail, rely on RX-only fallback

                        elif _media_check_sent and not _media_check_verified:
                            clean_line = event.line.strip()
                            if clean_line == _MEDIA_CHECK_TOKEN_OK:
                                _media_check_verified = True
                                logger.info('[TEZI] Removable media detected! Injecting live log tracker...')
                                try:
                                    serial_client.raw_write(
                                        b'( i=0; while [ ! -f /var/volatile/tezi.log ] && [ $i -lt 30 ]; do sleep 1; i=$((i+1)); done; '
                                        b'[ -f /var/volatile/tezi.log ] && tail -n +1 -f /var/volatile/tezi.log ) &\n'
                                    )
                                    tail_sent = True
                                except Exception as e:
                                    logger.warning('uart_write_blocked_e', e=e)
                            elif clean_line == _MEDIA_CHECK_TOKEN_MISSING:
                                logger.critical('[TEZI] Removable media check failed: USB-SD-Mux is missing or not fully plugged!')
                                raise ProvisioningError("No removable media detected (e.g. USB-SD-Mux is missing or not fully plugged in)!")

                logger.critical(
                    'tezi_fatal_failed_to_complete_installation_within_flash_timeout_s_s',
                    flash_timeout_s=self.flash_timeout_s,
                )
                return False
            finally:
                logger.info('[TEZI] Cleaning up and disconnecting serial client...')
                try:
                    serial_client.disconnect()
                except Exception as e:
                    logger.warning('failed_to_disconnect_serial_client_in_tezi_provision', error=str(e))

        # No serial_client supplied — uuu exit code 0 is sufficient proof of success.
        return True