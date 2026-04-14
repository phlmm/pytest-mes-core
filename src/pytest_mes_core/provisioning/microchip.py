# src/pytest_mes_core/provisioning/microchip.py
import logging
from pathlib import Path

from pytest_mes_core.provisioning.base import (
    BaseProvisioner,
    ProvisioningError,
    ImageVerificationError,
    SiliconLockError
)
from pytest_mes_core.host_adapters.microchip import HostPickitAdapter

# IMPORT THE NEW ENTERPRISE PRIMITIVE
from pytest_mes_core.utils.process import LiveProcess, ProcessTimeoutError, ProcessExecutionError

logger = logging.getLogger("mes_core.provisioning.microchip")

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
        timeout_s: int = 45
    ):
        self.pickit = pickit_adapter
        self.ipecmd_path = Path(ipecmd_path)
        self.device = device
        self.timeout_s = timeout_s

    def provision(self, image_path: Path) -> None:
        if not self.ipecmd_path.exists():
            err_msg = f"IPECMD Java archive not found at {self.ipecmd_path}."
            logger.critical(f"[ICSP] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)

        if not image_path.exists():
            err_msg = f"Firmware hex not found: {image_path}"
            logger.critical(f"[ICSP] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)

        logger.info(f"[ICSP] Initiating Java IPECMD flash of {self.device} with {image_path.name}...")

        # -M: Program entire device
        # -Y: Verify after programming
        cmd = [
            "java", "-jar", str(self.ipecmd_path),
            f"-P{self.device}",
            f"-TS{self.pickit.tool_serial}",
            f"-F{image_path.resolve()}",
            "-M", "-Y"
        ]

        try:
            # ==========================================
            # LIVE PROCESS EXECUTION & TELEMETRY
            # ==========================================
            process = LiveProcess(cmd, self.timeout_s, logger).execute()

            stdout_lower = process.stdout.lower()

            # ==========================================
            # IPECMD HEURISTIC ERROR MAPPING
            # ==========================================
            # 1. JVM Failures
            if "exception in thread" in stdout_lower or "outofmemoryerror" in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: Java Virtual Machine crashed! Trace saved to: {log_path}")
                raise ProvisioningError("IPECMD JVM crashed. Check Host PC RAM and Java version.")

            # 2. Hardware / Target Missing (Very common if test jig pins don't make contact)
            if "target device was not found" in stdout_lower or "connection failed" in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: PICkit could not detect the {self.device}. Pogo pin failure? Trace saved to: {log_path}")
                raise ProvisioningError("Target silicon not detected. Check physical ICSP connections and VDD.")

            # 3. Verification Failures
            if "verify failed" in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: Flash VERIFY FAILED. Bad sector or noisy clock line? Trace saved to: {log_path}")
                raise ImageVerificationError("ICSP readback verification failed.")

            # 4. Silicon Configuration Locks
            if "protected" in stdout_lower or "code protect" in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: Silicon is read/write protected. Trace saved to: {log_path}")
                raise SiliconLockError("PIC Configuration Bits are locked. Cannot flash.")

            # 5. Generic Tool Failures
            if "programming failed" in stdout_lower or process.returncode != 0:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: IPECMD failed with code {process.returncode}. Trace saved to: {log_path}")
                raise ProvisioningError(f"IPECMD failed to program the device. Check logs for Device ID or VDD errors.")

            # 6. Require POSITIVE Confirmation (IPECMD sometimes exits 0 on internal script aborts)
            if "programming complete" not in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: Missing positive confirmation from IPECMD. Trace saved to: {log_path}")
                raise ProvisioningError("IPECMD returned exit code 0, but did not confirm programming was complete.")

            logger.info(f"\n[ICSP] Successfully flashed and verified {self.device} in {process.duration_s}s.")

        except ProcessTimeoutError:
            raise ProvisioningError(f"PIC flash operation timed out after {self.timeout_s}s. JVM Deadlock.")
        except ProcessExecutionError as e:
            err_msg = f"Failed to execute IPECMD. Is Java installed? Error: {e}"
            logger.critical(f"\n[ICSP] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)
