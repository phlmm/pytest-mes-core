# src/pytest_mes_core/protocols/executable.py
import logging
from pathlib import Path
from typing import Dict, Any, Optional

from pytest_mes_core.transports import (
    DutTransport,
    HostSideBuffer,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import ExecutableConfig

logger = logging.getLogger("mes_core.protocols.exec")

# POSIX Fatal Signal Map (128 + Signal Number)
FATAL_SIGNALS = {
    132: "SIGILL (Illegal Instruction - Compiled for wrong ARM architecture?)",
    134: "SIGABRT (Abort - Failed C assertion or glibc panic)",
    135: "SIGBUS (Bus Error - Bad physical memory alignment)",
    136: "SIGFPE (Floating Point Exception - Division by zero)",
    137: "SIGKILL (Assassinated by Linux OOM Killer)",
    139: "SIGSEGV (Segmentation Fault - Null pointer or buffer overflow)"
}

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
        full_cmd = f"{cfg.binary_path} {cfg.arguments}".strip()

        logger.info(f"[Payload] Executing custom binary: {binary_name} (Timeout: {cfg.timeout_s}s)")

        # 1. Pre-Flight File Check
        try:
            logger.debug(f"[Payload] Pre-flight check: Verifying {binary_name} is executable...")
            if not dut.safe_run(f"test -x {cfg.binary_path}", timeout_s=5.0).ok:
                err_msg = "Executable not found or missing execute (+x) permissions."
                logger.error(f"[Payload] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg)
        except TransportConnectionError as e:
            return ValidatorResult(passed=False, error_msg=f"Transport pipe shattered during pre-flight check: {e}")

        tailer: Optional[HostSideBuffer] = None
        context_data: Dict[str, Any] = {}

        try:
            # 2. Arm the Background Data Vacuum (Transport Agnostic!)
            if cfg.log_file_path:
                logger.debug(f"[Payload] Arming background vacuum for {cfg.log_file_path}...")
                tailer = HostSideBuffer(dut, cfg.log_file_path, poll_interval_s=1.0)
                tailer.start()

            # 3. Execute the payload
            logger.debug(f"[Payload] Spawning binary and blocking for up to {cfg.timeout_s}s...")
            res = dut.safe_run(full_cmd, timeout_s=cfg.timeout_s)

            # 4. Contextualize standard streams
            context_data["exit_code"] = res.exited
            context_data["stdout_tail"] = res.stdout.strip()[-500:] if res.stdout else ""
            context_data["stderr_tail"] = res.stderr.strip()[-500:] if res.stderr else ""

            # 5. Fetch Custom Vacuumed Log
            if tailer:
                logger.debug("[Payload] Disarming vacuum and retrieving payload logs...")
                surviving_lines = tailer.stop()
                if surviving_lines:
                    context_data["custom_log_tail"] = "\n".join(surviving_lines)[-1000:]
            elif cfg.log_file_path:
                # Fallback if HostSideBuffer was disabled via TOML but a path was provided
                log_res = dut.safe_run(f"cat {cfg.log_file_path} 2>/dev/null", timeout_s=10.0)
                if log_res.ok and log_res.stdout:
                    context_data["custom_log_tail"] = log_res.stdout.strip()[-1000:]

            # ==========================================
            # 6. FORENSIC KERNEL INTERCEPTOR
            # ==========================================
            if res.exited in FATAL_SIGNALS:
                sig_name = FATAL_SIGNALS[res.exited]

                logger.critical("="*60)
                logger.critical(f"[Payload] FATAL: CRITICAL HARDWARE/OS FAULT DETECTED!")
                logger.critical(f"[Payload] {binary_name} crashed violently with: {sig_name}")

                # Perform an immediate sweep of the kernel ring buffer for the trap registers
                logger.critical("[Payload] Scraping dmesg for kernel trap registers...")
                dmesg_cmd = f"dmesg | grep -E '{binary_name}|traps:|Out of memory' | tail -n 20"
                dmesg_res = dut.safe_run(dmesg_cmd, timeout_s=5.0)

                if dmesg_res.ok and dmesg_res.stdout:
                    context_data["kernel_trap_trace"] = dmesg_res.stdout.strip()
                    logger.critical(f"[Payload] Kernel Trap Trace:\n{context_data['kernel_trap_trace']}")
                else:
                    context_data["kernel_trap_trace"] = "No dmesg trace found. Kernel may have frozen."
                    logger.critical("[Payload] No kernel trace found. Massive system fault possible.")

                logger.critical("="*60)

                return ValidatorResult(
                    passed=False,
                    error_msg=f"Binary crashed violently: {sig_name}",
                    context=context_data
                )

            # 7. Evaluate logical success criteria
            if res.exited != cfg.expected_exit_code:
                logger.error(f"[Payload] Logical Failure: Exited {res.exited} (Expected: {cfg.expected_exit_code})")
                return ValidatorResult(
                    passed=False,
                    error_msg=f"Binary exited with code {res.exited} (Expected: {cfg.expected_exit_code})",
                    context=context_data
                )

            logger.info(f"[Payload] Binary executed successfully in {round(res.duration_s, 3)}s.")
            return ValidatorResult(
                passed=True,
                metrics={f"t_{binary_name}_exec_s": res.duration_s},
                context=context_data
            )

        except TransportTimeoutError:
            # 8. Timeout Intercept (Silicon / Application Lockup)
            error_msg = f"Binary {binary_name} locked up and failed to return within {cfg.timeout_s}s."
            logger.critical("="*60)
            logger.critical(f"[Payload] FATAL: {error_msg}")

            # The vacuum might still have snagged the last thing the binary logged before it froze
            if tailer:
                surviving_lines = tailer.stop()
                if surviving_lines:
                    context_data["custom_log_tail_SURVIVED"] = "\n".join(surviving_lines)[-1000:]
                    logger.critical(f"[Payload] Vacuum rescued {len(surviving_lines)} lines of telemetry right before the freeze!")

            logger.critical("="*60)
            return ValidatorResult(passed=False, error_msg=error_msg, context=context_data)

        except TransportConnectionError as e:
            # 9. Transport Drop Intercept (Kernel Panic / Power Loss)
            error_msg = f"Transport pipe shattered while executing {binary_name}: {e}"
            logger.critical("="*60)
            logger.critical(f"[Payload] FATAL: CATASTROPHE DETECTED!")
            logger.critical(f"[Payload] {error_msg}")

            # The vacuum thread caught the data right before the socket died!
            if tailer:
                surviving_lines = tailer.stop()
                if surviving_lines:
                    context_data["custom_log_tail_SURVIVED"] = "\n".join(surviving_lines)[-1000:]
                    logger.critical(f"[Payload] Vacuum successfully smuggled {len(surviving_lines)} lines of telemetry out of the dying DUT!")

            logger.critical("="*60)
            return ValidatorResult(passed=False, error_msg=error_msg, context=context_data)

        finally:
            # 10. ZERO-LEAKAGE TEARDOWN
            if dut.is_connected:
                try:
                    logger.debug(f"[Payload] ZERO-LEAKAGE: Reaping '{binary_name}' and clearing logs...")
                    dut.safe_run(f"killall -9 {binary_name} >/dev/null 2>&1 || true", timeout_s=5.0)
                    if cfg.log_file_path:
                        dut.safe_run(f"rm -f {cfg.log_file_path} >/dev/null 2>&1 || true", timeout_s=5.0)
                except Exception as teardown_err:
                    logger.debug(f"[Payload] Cleanup failed (Transport likely dying): {teardown_err}")
