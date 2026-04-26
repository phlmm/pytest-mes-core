import structlog
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from pytest_mes_core.transports import DutTransport, HostSideBuffer, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import ExecutableConfig
logger = structlog.get_logger('mes_core.protocols.exec')
FATAL_SIGNALS = {132: 'SIGILL (Illegal Instruction - Compiled for wrong ARM architecture?)', 134: 'SIGABRT (Abort - Failed C assertion or glibc panic)', 135: 'SIGBUS (Bus Error - Bad physical memory alignment)', 136: 'SIGFPE (Floating Point Exception - Division by zero)', 137: 'SIGKILL (Assassinated by Linux OOM Killer)', 139: 'SIGSEGV (Segmentation Fault - Null pointer or buffer overflow)'}

class CustomPayloadValidator:
    """The 'Escape Hatch' protocol.

    Executes proprietary binaries safely, featuring automatic Kernel Trap interception,
    POSIX signal decoding, and asynchronous log vacuuming via HostSideBuffer.
    """

    @staticmethod
    def run_binary(dut: DutTransport, cfg: ExecutableConfig) -> ValidatorResult:
        """Executes a custom binary on the target and validates the result.

        Args:
            dut: The transport interface connected to the target.
            cfg: The execution configuration containing binary path, arguments, and timeouts.

        Returns:
            ValidatorResult: Pass/fail outcome based on expected exit codes, including
                execution time metrics and contextual stdout/stderr logs.
        """
        binary_name = Path(cfg.binary_path).name
        full_cmd = f'{cfg.binary_path} {cfg.arguments}'.strip()
        logger.info('executing_custom_binary_binary_name_timeout_timeout_s_s', binary_name=binary_name, timeout_s=cfg.timeout_s)
        try:
            logger.debug('pre_flight_check_verifying_binary_name_is_executable', binary_name=binary_name)
            if not dut.safe_run(f'test -x {cfg.binary_path}', timeout_s=5.0).ok:
                err_msg = 'Executable not found or missing execute (+x) permissions.'
                logger.error('err_msg', err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg)
        except TransportConnectionError as e:
            return ValidatorResult(passed=False, error_msg=f'Transport pipe shattered during pre-flight check: {e}')
        tailer: Optional[HostSideBuffer] = None
        context_data: Dict[str, Any] = {}
        try:
            if cfg.log_file_path:
                logger.debug('arming_background_vacuum_for_log_file_path', log_file_path=cfg.log_file_path)
                tailer = HostSideBuffer(dut, cfg.log_file_path, poll_interval_s=1.0)
                tailer.start()
            logger.debug('spawning_binary_and_blocking_for_up_to_timeout_s_s', timeout_s=cfg.timeout_s)
            res = dut.safe_run(full_cmd, timeout_s=cfg.timeout_s)
            context_data['exit_code'] = res.exited
            context_data['stdout_tail'] = res.stdout.strip()[-500:] if res.stdout else ''
            context_data['stderr_tail'] = res.stderr.strip()[-500:] if res.stderr else ''
            if tailer:
                logger.debug('[Payload] Disarming vacuum and retrieving payload logs...')
                surviving_lines = tailer.stop()
                if surviving_lines:
                    context_data['custom_log_tail'] = '\n'.join(surviving_lines)[-1000:]
            elif cfg.log_file_path:
                log_res = dut.safe_run(f'cat {cfg.log_file_path} 2>/dev/null', timeout_s=10.0)
                if log_res.ok and log_res.stdout:
                    context_data['custom_log_tail'] = log_res.stdout.strip()[-1000:]
            if res.exited in FATAL_SIGNALS:
                sig_name = FATAL_SIGNALS[res.exited]
                logger.critical('=' * 60)
                logger.critical('fatal_critical_hardware_os_fault_detected')
                logger.critical('binary_name_crashed_violently_with_sig_name', binary_name=binary_name, sig_name=sig_name)
                logger.critical('[Payload] Scraping dmesg for kernel trap registers...')
                dmesg_cmd = f"dmesg | grep -E '{binary_name}|traps:|Out of memory' | tail -n 20"
                dmesg_res = dut.safe_run(dmesg_cmd, timeout_s=5.0)
                if dmesg_res.ok and dmesg_res.stdout:
                    context_data['kernel_trap_trace'] = dmesg_res.stdout.strip()
                    logger.critical('kernel_trap_trace_val', val=context_data['kernel_trap_trace'])
                else:
                    context_data['kernel_trap_trace'] = 'No dmesg trace found. Kernel may have frozen.'
                    logger.critical('[Payload] No kernel trace found. Massive system fault possible.')
                logger.critical('=' * 60)
                return ValidatorResult(passed=False, error_msg=f'Binary crashed violently: {sig_name}', context=context_data)
            if res.exited != cfg.expected_exit_code:
                logger.error('logical_failure_exited_exited_expected_expected_exit_code', exited=res.exited, expected_exit_code=cfg.expected_exit_code)
                return ValidatorResult(passed=False, error_msg=f'Binary exited with code {res.exited} (Expected: {cfg.expected_exit_code})', context=context_data)
            logger.info('binary_executed_successfully_in_val_s', val=round(res.duration_s, 3))
            return ValidatorResult(passed=True, metrics={f't_{binary_name}_exec_s': res.duration_s}, context=context_data)
        except TransportTimeoutError:
            error_msg = f'Binary {binary_name} locked up and failed to return within {cfg.timeout_s}s.'
            logger.critical('=' * 60)
            logger.critical('fatal_error_msg', error_msg=error_msg)
            if tailer:
                surviving_lines = tailer.stop()
                if surviving_lines:
                    context_data['custom_log_tail_SURVIVED'] = '\n'.join(surviving_lines)[-1000:]
                    logger.critical('vacuum_rescued_val_lines_of_telemetry_right_before_the_freeze', val=len(surviving_lines))
            logger.critical('=' * 60)
            return ValidatorResult(passed=False, error_msg=error_msg, context=context_data)
        except TransportConnectionError as e:
            error_msg = f'Transport pipe shattered while executing {binary_name}: {e}'
            logger.critical('=' * 60)
            logger.critical('fatal_catastrophe_detected')
            logger.critical('error_msg', error_msg=error_msg)
            if tailer:
                surviving_lines = tailer.stop()
                if surviving_lines:
                    context_data['custom_log_tail_SURVIVED'] = '\n'.join(surviving_lines)[-1000:]
                    logger.critical('vacuum_successfully_smuggled_val_lines_of_telemetry_out_of_the_dying_dut', val=len(surviving_lines))
            logger.critical('=' * 60)
            return ValidatorResult(passed=False, error_msg=error_msg, context=context_data)
        finally:
            if dut.is_connected:
                try:
                    logger.debug('zero_leakage_reaping_binary_name_and_clearing_logs', binary_name=binary_name)
                    dut.safe_run(f'killall -9 {binary_name} >/dev/null 2>&1 || true', timeout_s=5.0)
                    if cfg.log_file_path:
                        dut.safe_run(f'rm -f {cfg.log_file_path} >/dev/null 2>&1 || true', timeout_s=5.0)
                except Exception as teardown_err:
                    logger.debug('cleanup_failed_transport_likely_dying_teardown_err', teardown_err=teardown_err)