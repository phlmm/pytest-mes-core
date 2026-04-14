# src/pytest_mes_core/provisioning/tezi_uuu.py
import time
import logging
import subprocess
from pathlib import Path
from typing import Optional

from pytest_mes_core.provisioning.base import BaseProvisioner, ProvisioningError

# IMPORT THE NEW ENTERPRISE PRIMITIVE
from pytest_mes_core.utils.process import LiveProcess, ProcessTimeoutError, ProcessExecutionError

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
        """Polls the Linux USB tree to verify the SoC BootROM is visible."""
        try:
            # 1. Native OS Radar (Bypasses uuu permission/sudo traps)
            # Fast, silent programmatic commands stay as subprocess.run
            res_lsusb = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
            output = res_lsusb.stdout.lower()

            # 1fc9 is NXP. 15a2 is Freescale. Both cover the entire i.MX line in Serial Downloader mode.
            if "1fc9:" in output or "15a2:" in output or "nxp semiconductors" in output:
                return True

            # 2. Fallback to uuu (Check stderr as well, where uuu sometimes prints)
            res_uuu = subprocess.run(["uuu", "-lsusb"], capture_output=True, text=True, timeout=5)
            out_uuu = res_uuu.stdout.lower() + res_uuu.stderr.lower()

            if self.usb_path:
                return self.usb_path in out_uuu

            return "1:" in out_uuu or "nxp" in out_uuu

        except subprocess.TimeoutExpired:
            return False
        except FileNotFoundError:
            err_msg = "FATAL: 'lsusb' or 'uuu' tool is not installed on Host PC."
            logger.critical(f"[TEZI] {err_msg}")
            raise ProvisioningError(err_msg)

    def provision(self, image_path: Path) -> None:
        """
        Hardware Flow:
            1. Blocks until the DUT physical USB enumerates in NXP Recovery Mode.
            2. Executes `uuu` targeting the specific USB port and TEZI folder.
            3. Uses LiveProcess to handle telemetry and artifact dumping.
        """
        tezi_dir = image_path

        if not tezi_dir.exists() or not tezi_dir.is_dir():
            err_msg = f"TEZI payload directory not found: {tezi_dir}"
            logger.critical(f"[TEZI] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)

        if not (tezi_dir / "uuu.auto").exists():
            err_msg = f"Invalid TEZI payload (missing uuu.auto inside {tezi_dir})."
            logger.critical(f"[TEZI] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)

        target_str = f" on USB port {self.usb_path}" if self.usb_path else ""
        logger.info(f"[TEZI] Waiting up to {self.wait_for_recovery_s}s for DUT to enter Recovery Mode{target_str}...")

        # 1. Defeat OS Jitter / Operator Delay
        t_wait_start = time.perf_counter()
        device_found = False
        while time.perf_counter() - t_wait_start < self.wait_for_recovery_s:
            logger.debug("[TEZI] Polling USB bus for NXP BootROM...")
            if self._is_device_in_recovery():
                device_found = True
                break
            time.sleep(0.5) # Fast poll to snap execution instantly

        if not device_found:
            err_msg = f"Timeout waiting for USB Recovery mode{target_str}. Is the boot jumper set?"
            logger.critical(f"[TEZI] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)

        logger.info(f"[TEZI] DUT detected! Injecting TEZI payload from {tezi_dir.name}...")

        # 2. Execute uuu securely
        cmd = ["uuu"]
        if self.usb_path:
            cmd.extend(["-m", self.usb_path])
        cmd.append(str(tezi_dir.absolute()))

        try:
            # ==========================================
            # LIVE PROCESS EXECUTION & TELEMETRY
            # ==========================================
            process = LiveProcess(cmd, self.flash_timeout_s, logger).execute()

            # 3. Analyze output physics
            combined_lower = process.stdout.lower()

            if process.returncode != 0 or "libusb_open" in combined_lower or "access denied" in combined_lower:
                # EXPORT ARTIFACT
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[TEZI] FATAL: uuu rejected the payload! (Code {process.returncode}). Trace saved to: {log_path}")

                if "libusb" in combined_lower or "access" in combined_lower or "permission" in combined_lower:
                    raise ProvisioningError(
                        "OS Permission Denied! You must either run pytest with 'sudo' "
                        "or install the NXP udev rules so your user can access the USB device."
                    )
                raise ProvisioningError(f"uuu lost USB sync (Code {process.returncode}).")

            if "error" in combined_lower or ("done" not in combined_lower and "success" not in combined_lower):
                # EXPORT ARTIFACT
                log_path = process.export_log(Path("/tmp/mes_artifacts"))
                logger.critical(f"\n[TEZI] FATAL: uuu falsely exited 0. Payload never executed. Trace saved to: {log_path}")
                raise ProvisioningError("uuu script failed to execute fully. Missing 'Done' confirmation.")

            logger.info(f"\n[TEZI] Flash successfully pushed to SoC RAM in {process.duration_s}s.")

        except ProcessTimeoutError:
            raise ProvisioningError(f"uuu execution timed out after {self.flash_timeout_s}s! USB EMI reset?")
        except ProcessExecutionError as e:
            err_msg = f"Failed to execute uuu command: {e}"
            logger.critical(f"[TEZI] FATAL: {err_msg}")
            raise ProvisioningError(err_msg)
