# src/pytest_mes_core/provisioning/tezi_uuu.py
import time
import logging
import subprocess
from pathlib import Path
from typing import Optional

from pytest_mes_core.provisioning.base import BaseProvisioner, ProvisioningError

logger = logging.getLogger("mes_core.provisioning.tezi")

class UuuTeziProvisioner(BaseProvisioner):
    """
    Zero-leakage manager for the NXP Universal Update Utility (uuu).
    Pushes TEZI images into SoC RAM via USB Serial Downloader mode.
    Enforces USB port isolation for parallel multi-jig environments.
    """
    def __init__(
        self,
        wait_for_recovery_s: int = 30,
        flash_timeout_s: int = 300,
        usb_path: Optional[str] = None
    ):
        self.wait_for_recovery_s = wait_for_recovery_s
        self.flash_timeout_s = flash_timeout_s
        # Expected format: "1:2" or "2:1.4" (matches `uuu -lsusb` output)
        self.usb_path = usb_path

    def _is_device_in_recovery(self) -> bool:
        """Polls `uuu -lsusb` to verify the specific SoC BootROM is visible."""
        try:
            res = subprocess.run(["uuu", "-lsusb"], capture_output=True, text=True, timeout=5)

            # 1. Multi-Jig Isolation Check
            if self.usb_path:
                return self.usb_path in res.stdout

            # 2. Single-Jig Fallback
            return "1:" in res.stdout or "Toradex" in res.stdout or "NXP" in res.stdout

        except subprocess.TimeoutExpired:
            return False
        except FileNotFoundError:
            raise ProvisioningError("FATAL: 'uuu' tool is not installed or not in the system PATH.")

    def provision(self, image_path: Path) -> None:
        """
        Hardware Flow:
            1. Blocks until the DUT physical USB enumerates in NXP Recovery Mode.
            2. Executes `uuu` targeting the specific USB port and TEZI folder.
            3. Violently reaps the `uuu` process on timeout to prevent Host PC deadlocks.
        """
        # For TEZI, the 'image_path' is actually the directory containing uuu.auto
        tezi_dir = image_path

        if not tezi_dir.exists() or not tezi_dir.is_dir():
            raise ProvisioningError(f"TEZI payload directory not found: {tezi_dir}")

        if not (tezi_dir / "uuu.auto").exists():
            raise ProvisioningError(f"Invalid TEZI payload (missing uuu.auto inside {tezi_dir}).")

        target_str = f" on USB port {self.usb_path}" if self.usb_path else ""
        logger.info(f"[TEZI] Waiting up to {self.wait_for_recovery_s}s for DUT to enter Recovery Mode{target_str}...")

        # 1. Defeat OS Jitter / Operator Delay
        t_wait_start = time.perf_counter()
        device_found = False
        while time.perf_counter() - t_wait_start < self.wait_for_recovery_s:
            if self._is_device_in_recovery():
                device_found = True
                break
            time.sleep(1.0)

        if not device_found:
            raise ProvisioningError(f"Timeout waiting for USB Recovery mode{target_str}. Is the boot jumper set?")

        logger.info(f"[TEZI] DUT detected. Injecting TEZI payload from {tezi_dir.name}...")

        # 2. Execute uuu securely with Port Isolation
        cmd = ["uuu"]
        if self.usb_path:
            cmd.extend(["-m", self.usb_path])
        cmd.append(str(tezi_dir.absolute()))

        t0 = time.perf_counter()

        # Spawn using Popen to enforce violent destruction in finally block
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            stdout, stderr = proc.communicate(timeout=self.flash_timeout_s)
        except subprocess.TimeoutExpired:
            logger.critical(f"[TEZI] FATAL: uuu hung for >{self.flash_timeout_s}s! USB EMI reset? Killing process.")
            proc.kill()
            proc.communicate() # Reap the zombie
            raise ProvisioningError(f"uuu execution timed out after {self.flash_timeout_s}s.")
        finally:
            # ZERO-LEAKAGE: Catch-all if the Python thread panics for an unrelated reason
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        duration = round(time.perf_counter() - t0, 3)

        # 3. Analyze output physics
        if proc.returncode != 0:
            logger.error(f"[TEZI] uuu failed with code {proc.returncode}. Stderr:\n{stderr.strip()}")
            raise ProvisioningError(f"uuu rejected the payload or lost USB sync (Code {proc.returncode}).")

        # uuu sometimes exits 0 even if it fails to write if a script error occurs. We verify the stdout log.
        stdout_lower = stdout.lower()
        if "error" in stdout_lower or ("done" not in stdout_lower and "success" not in stdout_lower):
            logger.error(f"[TEZI] uuu falsely exited 0. Payload never executed.\n{stdout}")
            raise ProvisioningError("uuu script failed to execute fully. Missing 'Done' confirmation.")

        logger.info(f"[TEZI] Flash successfully pushed to SoC RAM in {duration}s.")
