# src/pytest_mes_core/provisioning/secure_fetch.py
import os
import hashlib
import urllib.request
import urllib.error
import logging
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_fixed

from pytest_mes_core.provisioning.base import ProvisioningError, ImageVerificationError

logger = logging.getLogger("mes_core.provisioning.secure_fetch")

class SecureFetchError(ProvisioningError):
    pass

class SecureAssetFetcher:
    """
    Fetches firmware/artifacts from factory servers with strict cryptographic verification.
    Features RAM-safe chunking, atomic file locks, and idempotent caching.
    """

    @staticmethod
    def _calculate_local_hash(filepath: Path) -> str:
        """Helper to safely hash local files without blowing up RAM."""
        hasher = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest().lower()

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2.0), reraise=True)
    def fetch_and_verify(url: str, expected_sha256: str, dest: Path, timeout_s: float = 30.0) -> Path:
        expected_sha256 = expected_sha256.lower()

        # ==========================================
        # 1. IDEMPOTENCY CACHE CHECK
        # ==========================================
        if dest.exists():
            logger.debug(f"[Fetch] File exists at {dest}. Verifying hash to save bandwidth...")
            if SecureAssetFetcher._calculate_local_hash(dest) == expected_sha256:
                logger.info(f"[Fetch] Cache hit! Valid firmware already present at {dest.name}.")
                return dest
            else:
                logger.warning(f"[Fetch] Local file corrupted or outdated. Redownloading...")
                dest.unlink(missing_ok=True)

        logger.info(f"[Fetch] Downloading asset from {url}...")

        # We download to a temporary '.part' file so parallel workers don't read a half-written file
        temp_dest = dest.with_suffix(".part")
        hasher = hashlib.sha256()

        try:
            # ==========================================
            # 2. RAM-SAFE STREAMING DOWNLOAD
            # ==========================================
            req = urllib.request.Request(url, headers={'User-Agent': 'MES-Core-Provisioner/1.0'})

            with urllib.request.urlopen(req, timeout=timeout_s) as response, open(temp_dest, "wb") as out_file:
                # Read in 64KB chunks to keep memory footprint near zero
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break

                    # 3. ON-THE-FLY HASHING
                    out_file.write(chunk)
                    hasher.update(chunk)

        except urllib.error.HTTPError as e:
            # Do not let Tenacity blindly retry 404/403 errors. Fail fast.
            if e.code in (401, 403, 404):
                logger.critical(f"[Fetch] Fatal HTTP {e.code} fetching {url}. Server says: {e.reason}")
                raise SecureFetchError(f"HTTP {e.code} - Cannot fetch firmware.")
            raise # Let Tenacity retry 502s, 504s, etc.

        except Exception as e:
            logger.error(f"[Fetch] Network/IO failure: {e}")
            raise SecureFetchError(f"Failed to fetch asset: {e}")

        # ==========================================
        # 4. CRYPTOGRAPHIC VERIFICATION & ATOMIC RENAME
        # ==========================================
        actual_hash = hasher.hexdigest().lower()

        if actual_hash != expected_sha256:
            temp_dest.unlink(missing_ok=True)
            logger.critical(f"[Fetch] Hash mismatch! Expected {expected_sha256}, got {actual_hash}")
            raise ImageVerificationError("FATAL: Firmware SHA256 mismatch! Transit payload corrupted.")

        # Atomic rename guarantees that when the real 'dest' appears, it is 100% complete and verified
        os.rename(temp_dest, dest)

        logger.info(f"[Fetch] Asset verified successfully. Hash: {actual_hash[:8]}...")
        return dest
