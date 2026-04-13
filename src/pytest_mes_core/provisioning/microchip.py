# src/pytest_mes_core/provisioning/microchip.py
import logging
import subprocess
from pathlib import Path

from pytest_mes_core.provisioning.base import (
    BaseProvisioner,
    ProvisioningError,
    ImageVerificationError,
    SiliconLockError
)
from pytest_mes_core.host_adapters.microchip import HostPickitAdapter

logger = logging.getLogger("mes_core.provisioning.microchip")

class MicrochipIpeProvisioner(BaseProvisioner):
    """
    Executes Microchip's IPECMD tool over a securely locked Host Adapter.
    Validates output defensively and ensures deep observability for hardware failures.
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
            raise ProvisioningError(f"IPECMD jar not found at {self.ipecmd_path}.")
        if not image_path.exists():
            raise ProvisioningError(f"Firmware hex not found: {image_path}")

        logger.info(f"[ICSP] Flashing {self.device} with {image_path.name} via {self.pickit.tool_serial}...")

        cmd = [
            "java", "-jar", str(self.ipecmd_path),
            f"-P{self.device}",
            f"-TS{self.pickit.tool_serial}",
            f"-F{image_path.resolve()}",
            "-M", "-Y"
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s)
            stdout_lower = result.stdout.lower()

            # 1. Check for Verification Failures
            if "verify failed" in stdout_lower:
                logger.critical(f"[ICSP] Flash VERIFY FAILED. Raw Output:\n{result.stdout}")
                raise ImageVerificationError("ICSP readback verification failed. Bad sector or noisy clock line.")

            # 2. Check for Hardware Locks
            if "protected" in stdout_lower or "code protect" in stdout_lower:
                logger.error(f"[ICSP] Silicon locked. Raw Output:\n{result.stdout}")
                raise SiliconLockError("PIC18 Configuration Bits are locked.")

            # 3. Check for Explicit Tool Failures (Expose the Root Cause!)
            if "programming failed" in stdout_lower or result.returncode != 0:
                logger.error(f"[ICSP] IPECMD failed with code {result.returncode}. Raw Output:\n{result.stdout}")
                raise ProvisioningError(f"IPECMD failed to program the device. See logs for Device ID or VDD errors.")

            # 4. Require POSITIVE Confirmation
            if "programming complete" not in stdout_lower:
                logger.error(f"[ICSP] Missing positive confirmation. Raw Output:\n{result.stdout}")
                raise ProvisioningError("IPECMD returned 0, but did not confirm programming was complete.")

            logger.info(f"[ICSP] Successfully flashed and verified {self.device}.")

        except subprocess.TimeoutExpired:
            logger.critical(f"[ICSP] IPECMD hung for >{self.timeout_s}s.")
            raise ProvisioningError(f"PIC18 flash operation timed out after {self.timeout_s} seconds.")
        except FileNotFoundError:
            logger.error("[ICSP] Java executable not found in PATH.")
            raise ProvisioningError("Java is not installed or not in the system PATH. IPECMD requires Java.")
