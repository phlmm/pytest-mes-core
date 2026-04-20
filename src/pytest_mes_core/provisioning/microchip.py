import logging
from pathlib import Path
from typing import List, Optional

from pytest_mes_core.provisioning.base import (
    BaseProvisioner,
    ProvisioningError,
    ImageVerificationError,
    SiliconLockError
)
from pytest_mes_core.host_adapters.microchip import HostPickitAdapter
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
        timeout_s: int = 45,
        extra_flags: Optional[List[str]] = None
    ):
        self.pickit = pickit_adapter
        self.ipecmd_path = Path(ipecmd_path)
        self.device = device
        self.timeout_s = timeout_s
        self.extra_flags = extra_flags or []

    # 🚨 Syncing signature to the new base class contract
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
            err_msg = f"IPECMD executable not found at {self.ipecmd_path}."
            logger.critical(f"[ICSP] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)

        if not image_path.exists():
            err_msg = f"Firmware hex not found: {image_path}"
            logger.critical(f"[ICSP] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)

        logger.info(f"[ICSP] Initiating IPECMD flash of {self.device} with {image_path.name}...")

        cmd = []
        if self.ipecmd_path.suffix == ".jar":
            cmd.extend(["java", "-jar", str(self.ipecmd_path)])
        else:
            cmd.append(str(self.ipecmd_path))

        cmd.extend([
            f"-P{self.device}",
            f"-TS{self.pickit.tool_serial}",
            f"-F{image_path.resolve()}",
            "-M", "-Y"
        ])

        if self.extra_flags:
            cmd.extend(self.extra_flags)

        try:
            process = LiveProcess(cmd, self.timeout_s, logger).execute()
            stdout_lower = process.stdout.lower()

            if "no voltage has been detected on vdd" in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: Target is unpowered! Add '-W3.3' to extra_flags or power the board. Trace: {log_path}")
                raise ProvisioningError("Target VDD missing. ICSP refused to connect.")

            if "exception in thread" in stdout_lower or "outofmemoryerror" in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: Java Virtual Machine crashed! Trace saved to: {log_path}")
                raise ProvisioningError("IPECMD JVM crashed. Check Host PC RAM and Java version.")

            if "target device was not found" in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: PICkit could not detect the {self.device}. Pogo pin failure? Trace saved to: {log_path}")
                raise ProvisioningError("Target silicon not detected. Check physical ICSP connections and VDD.")

            if "verify failed" in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: Flash VERIFY FAILED. Bad sector or noisy clock line? Trace: {log_path}")
                raise ImageVerificationError("ICSP readback verification failed.")

            if "protected" in stdout_lower or "code protect" in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: Silicon is read/write protected. Trace saved to: {log_path}")
                raise SiliconLockError("PIC Configuration Bits are locked. Cannot flash.")

            if "program succeeded" not in stdout_lower and "programming complete" not in stdout_lower:
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[ICSP] FATAL: Missing positive confirmation from IPECMD. Trace saved to: {log_path}")
                raise ProvisioningError("IPECMD returned exit code 0, but did not confirm programming was complete.")

            logger.info(f"\n[ICSP] Successfully flashed and verified {self.device} in {process.duration_s}s.")

            return True

        except ProcessTimeoutError:
            raise ProvisioningError(f"PIC flash operation timed out after {self.timeout_s}s. JVM Deadlock.")
        except ProcessExecutionError as e:
            err_msg = f"Failed to execute IPECMD script. Error: {e}"
            logger.critical(f"\n[ICSP] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)
