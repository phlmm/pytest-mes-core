# src/pytest_mes_core/provisioning/block_device.py
import subprocess
import time
import logging
from pathlib import Path
from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.provisioning.block_device")

class BlockDeviceProvisioner:
    """Flashes block devices connected to the Host PC (via USB-SD-Mux or direct USB)."""

    @staticmethod
    def flash_image_bmap(
        image_path: Path,
        host_block_device: str,
        timeout_s: int = 300
    ) -> ValidatorResult:
        """
        Hardware Flow:
            Uses bmaptool to securely and rapidly flash an image to physical media.
            Enforces a final POSIX 'sync' to flush RAM caches to silicon.
        """
        if not image_path.exists():
            return ValidatorResult(passed=False, error_msg=f"Image missing at {image_path}")

        logger.info(f"[Provisioning] Initiating bmaptool flash of {image_path.name} to {host_block_device}...")

        # bmaptool automatically handles bmap file resolution and handles sync internally,
        # but we add explicit sync for absolute zero-defect paranoia.
        cmd = ["bmaptool", "copy", str(image_path), host_block_device]

        t0 = time.perf_counter()

        try:
            # We use Popen/communicate to capture output live if needed, or just run for blocking
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            logger.error("[Provisioning] bmaptool flash TIMEOUT.")
            return ValidatorResult(passed=False, error_msg=f"Flash timeout after {timeout_s}s.")

        if res.returncode != 0:
            logger.error(f"[Provisioning] bmaptool failed: {res.stderr}")
            return ValidatorResult(passed=False, error_msg="bmaptool execution failed.")

        # 2. Defeat OS Caching (Critical for USB-SD-Mux before switching)
        logger.debug("[Provisioning] Forcing kernel sync to flush buffers to physical SD silicon...")
        subprocess.run(["sync"], check=True)

        duration = round(time.perf_counter() - t0, 3)
        logger.info(f"[Provisioning] Flash completed and synced in {duration}s.")

        return ValidatorResult(
            passed=True,
            metrics={"t_flash_s": duration},
            context={"image": image_path.name}
        )
