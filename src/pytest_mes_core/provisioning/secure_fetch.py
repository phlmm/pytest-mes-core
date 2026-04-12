# src/pytest_mes_core/provisioning/secure_fetch.py
import hashlib
import urllib.request
import logging
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_fixed

logger = logging.getLogger("mes_core.provisioning.fetch")

class SecureAssetFetcher:
    """Fetches firmware/artifacts from factory servers with strict cryptographic verification."""

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2.0), reraise=True)
    def fetch_and_verify(url: str, expected_sha256: str, dest: Path, timeout_s: float = 15.0) -> Path:
        logger.info(f"[Provisioning] Fetching asset from {url}...")

        try:
            # Replaced urlretrieve with urlopen to enforce strict network timeouts
            with urllib.request.urlopen(url, timeout=timeout_s) as response, open(dest, "wb") as out_file:
                out_file.write(response.read())
        except Exception as e:
            logger.error(f"[Provisioning] Network fetch failed: {e}")
            raise RuntimeError(f"Failed to fetch asset from {url}: {e}")

        # Compute Hash
        hasher = hashlib.sha256()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)

        actual_hash = hasher.hexdigest().lower()

        if actual_hash != expected_sha256.lower():
            dest.unlink(missing_ok=True) # Delete the corrupted file
            logger.critical(f"[Provisioning] Hash mismatch! Expected {expected_sha256}, got {actual_hash}")
            raise ValueError("FATAL: Firmware SHA256 mismatch! Payload corrupted.")

        logger.info(f"[Provisioning] Asset verified. Hash: {actual_hash}")
        return dest
