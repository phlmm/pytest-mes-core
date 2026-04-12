import time
import logging
from pathlib import Path
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult
from pytest_mes_core.config import ExecutableConfig

logger = logging.getLogger("mes_core.protocols.exec")

class CustomPayloadValidator:
    """
    The 'Escape Hatch' protocol.
    Executes proprietary, third-party, or custom C/Go/Rust binaries on the DUT safely.
    Enforces strict timeouts and zero-leakage process reaping.
    """

    @staticmethod
    def run_binary(dut_ssh: EphemeralSSHClient, cfg: ExecutableConfig) -> ValidatorResult:
        binary_name = Path(cfg.binary_path).name
        full_cmd = f"{cfg.binary_path} {cfg.arguments}".strip()

        logger.info(f"[Payload] Executing custom binary: {binary_name} (Timeout: {cfg.timeout_s}s)")
        logger.debug(f"[Payload] Full command: {full_cmd}")

        # 1. Check if binary exists and is executable
        if not dut_ssh.safe_run(f"test -x {cfg.binary_path}", timeout_s=5.0).ok:
            logger.error(f"[Payload] Binary missing or not executable at {cfg.binary_path}")
            return ValidatorResult(passed=False, error_msg="Executable not found or permissions denied.")

        t0 = time.perf_counter()

        try:
            # 2. Execute the payload
            # We use safe_run which inherently protects the SSH pipe
            res = dut_ssh.safe_run(full_cmd, timeout_s=cfg.timeout_s)
            duration = round(time.perf_counter() - t0, 3)

            # 3. Contextualize the output (Truncate to prevent JSONL telemetry bloat)
            stdout_trunc = res.stdout.strip()[-500:] if res.stdout else ""
            stderr_trunc = res.stderr.strip()[-500:] if res.stderr else ""

            context_data = {
                "exit_code": res.exited,
                "stdout_tail": stdout_trunc,
                "stderr_tail": stderr_trunc
            }

            # 4. Fetch the custom log file if specified
            if cfg.log_file_path:
                logger.debug(f"[Payload] Retrieving payload log from {cfg.log_file_path}...")
                log_res = dut_ssh.safe_run(f"cat {cfg.log_file_path} 2>/dev/null", timeout_s=10.0)
                if log_res.ok and log_res.stdout:
                    context_data["custom_log_tail"] = log_res.stdout.strip()[-1000:]
                else:
                    logger.warning(f"[Payload] Log file {cfg.log_file_path} was requested but not found.")

            # 5. Evaluate pass/fail criteria
            if res.exited != cfg.expected_exit_code:
                logger.error(f"[Payload] {binary_name} exited with {res.exited} (Expected {cfg.expected_exit_code}).")
                return ValidatorResult(
                    passed=False,
                    error_msg=f"Binary exited with code {res.exited}",
                    context=context_data
                )

            logger.info(f"[Payload] Execution completed successfully in {duration}s.")
            return ValidatorResult(
                passed=True,
                metrics={f"t_{binary_name}_exec_s": duration},
                context=context_data
            )

        finally:
            # 6. ZERO-LEAKAGE: The Safety Net
            # If the binary spawned detached children or didn't die cleanly, we reap it violently.
            logger.debug(f"[Payload] ZERO-LEAKAGE: Sweeping for zombie '{binary_name}' processes.")
            dut_ssh.conn.run(f"killall -9 {binary_name} >/dev/null 2>&1 || true", hide=True, warn=True)

            # Clean up the log file so it doesn't bleed into the next test iteration
            if cfg.log_file_path:
                dut_ssh.conn.run(f"rm -f {cfg.log_file_path} >/dev/null 2>&1 || true", hide=True, warn=True)
