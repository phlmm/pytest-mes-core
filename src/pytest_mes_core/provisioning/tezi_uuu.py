# src/pytest_mes_core/provisioning/tezi_uuu.py
import subprocess
import time
import logging
from pathlib import Path
from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.provisioning.tezi")

class UuuTeziProvisioner:
    """
    Zero-leakage manager for the NXP Universal Update Utility (uuu).
    Pushes TEZI images into SoC RAM via USB Serial Downloader mode.
    """

    @staticmethod
    def _is_device_in_recovery() -> bool:
        """Polls `uuu -lsusb` to verify the SoC BootROM is visible to the Host PC."""
        try:
            res = subprocess.run(["uuu", "-lsusb"], capture_output=True, text=True, timeout=5)
            # 'uuu -lsusb' lists Connected Known USB Devices. If empty, nothing is in recovery.
            return "1:" in res.stdout or "Toradex" in res.stdout or "NXP" in res.stdout
        except subprocess.TimeoutExpired:
            return False

    @staticmethod
    def flash_tezi_image(
        tezi_dir: Path,
        wait_for_recovery_s: int = 30,
        flash_timeout_s: int = 300
    ) -> ValidatorResult:
        """
        Hardware Flow:
            1. Blocks until the DUT physical USB enumerates in NXP Recovery Mode.
            2. Executes `uuu` targeting the TEZI folder.
            3. Violently reaps the `uuu` process on timeout to prevent Host PC deadlocks.
        """
        if not tezi_dir.exists() or not tezi_dir.is_dir():
            logger.error(f"[TEZI] Missing image directory: {tezi_dir}")
            return ValidatorResult(passed=False, error_msg=f"TEZI payload directory not found: {tezi_dir}")

        if not (tezi_dir / "uuu.auto").exists():
            logger.error(f"[TEZI] Missing 'uuu.auto' script inside {tezi_dir}")
            return ValidatorResult(passed=False, error_msg="Invalid TEZI payload (missing uuu.auto).")

        logger.info(f"[TEZI] Waiting up to {wait_for_recovery_s}s for DUT to enter USB Recovery Mode...")

        # 1. Defeat OS Jitter / Operator Delay
        t_wait_start = time.perf_counter()
        device_found = False
        while time.perf_counter() - t_wait_start < wait_for_recovery_s:
            if UuuTeziProvisioner._is_device_in_recovery():
                device_found = True
                break
            time.sleep(1.0)

        if not device_found:
            logger.error("[TEZI] TIMEOUT. Device never enumerated in Recovery Mode. Jumper missing?")
            return ValidatorResult(passed=False, error_msg="Timeout waiting for USB Recovery mode.")

        logger.info(f"[TEZI] DUT detected. Injecting TEZI payload from {tezi_dir}...")

        # 2. Execute uuu securely
        # 'uuu <folder>' automatically targets the uuu.auto script inside it
        cmd = ["uuu", str(tezi_dir.absolute())]

        t0 = time.perf_counter()

        # We spawn using Popen to enforce violent destruction in finally block
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            stdout, stderr = proc.communicate(timeout=flash_timeout_s)
        except subprocess.TimeoutExpired:
            logger.critical(f"[TEZI] FATAL: uuu hung for >{flash_timeout_s}s! USB EMI reset? Killing process.")
            proc.kill()
            proc.communicate() # Reap the zombie
            return ValidatorResult(passed=False, error_msg=f"uuu execution timed out after {flash_timeout_s}s.")
        finally:
            # ZERO-LEAKAGE: Catch-all to ensure we don't leave uuu running if the Python thread panics
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        duration = round(time.perf_counter() - t0, 3)

        # 3. Analyze output physics
        if proc.returncode != 0:
            logger.error(f"[TEZI] uuu failed with code {proc.returncode}. Stderr: {stderr.strip()}")
            return ValidatorResult(passed=False, error_msg="uuu rejected the payload or lost USB sync.")

        # uuu sometimes exits 0 even if it fails to write if a script error occurs. We verify the stdout log.
        if "Wait for Known USB Device Appear" in stdout and "Done" not in stdout:
            logger.error(f"[TEZI] uuu falsely exited 0. Payload never executed.\n{stdout}")
            return ValidatorResult(passed=False, error_msg="uuu script failed to execute fully.")

        logger.info(f"[TEZI] Flash successfully pushed to SoC RAM in {duration}s.")
        return ValidatorResult(
            passed=True,
            metrics={"t_tezi_uuu_flash_s": duration},
            context={"tezi_payload": tezi_dir.name}
        )
