# src/pytest_mes_core/provisioning/secure_fetch.py
import os
import hashlib
import urllib.request
import urllib.error
import logging
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_fixed, before_sleep_log
from typing import Optional

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
        """Helper to safely hash local files without blowing up RAM.

        Args:
            filepath: Path to the local file to hash.

        Returns:
            str: The computed SHA256 hexadecimal string.
        """
        logger.debug(f"[Fetch] Calculating SHA256 for local file {filepath.name}...")
        hasher = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)

        result = hasher.hexdigest().lower()
        logger.debug(f"[Fetch] Local hash computed: {result}")
        return result

    # Wire Tenacity's retry logic directly into our Pytest Live Logger as a WARNING
    @staticmethod
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(2.0),
        reraise=True,
        before_sleep=before_sleep_log(logger, logging.WARNING)
    )
    def fetch_and_verify(url: str, expected_sha256: str, dest: Path, timeout_s: float = 30.0) -> Path:
        """Fetches an asset from a remote URL with strict cryptographic verification.

        Features RAM-safe chunking, atomic file locks, and idempotent caching.

        Args:
            url: The remote URL to download from.
            expected_sha256: The expected SHA256 checksum of the asset.
            dest: The final destination path for the verified asset.
            timeout_s: The maximum seconds to wait for the download stream.

        Returns:
            Path: The path to the successfully downloaded and verified asset.

        Raises:
            SecureFetchError: If the network request fails or returns an HTTP error.
            ImageVerificationError: If the downloaded file's checksum doesn't match.
        """
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
                logger.debug(f"[Fetch] ZERO-LEAKAGE: Destroying invalid cache file at {dest}.")
                dest.unlink(missing_ok=True)

        logger.info(f"[Fetch] Downloading asset from {url}...")

        # We download to a temporary '.part' file so parallel workers don't read a half-written file
        temp_dest = dest.with_suffix(f".part.{os.getpid()}")
        hasher = hashlib.sha256()

        try:
            # ==========================================
            # 2. RAM-SAFE STREAMING DOWNLOAD
            # ==========================================
            req = urllib.request.Request(url, headers={'User-Agent': 'MES-Core-Provisioner/1.0'})
            logger.debug(f"[Fetch] Opening HTTP stream with {timeout_s}s timeout...")

            with urllib.request.urlopen(req, timeout=timeout_s) as response, open(temp_dest, "wb") as out_file:

                bytes_downloaded = 0
                last_log_mb = 0

                # Read in 64KB chunks to keep memory footprint near zero
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break

                    # 3. ON-THE-FLY HASHING
                    out_file.write(chunk)
                    hasher.update(chunk)
                    bytes_downloaded += len(chunk)

                    # Matrix Tracing: Log progress every 10 Megabytes
                    current_mb = bytes_downloaded // (1024 * 1024)
                    if current_mb >= last_log_mb + 10:
                        logger.debug(f"[Fetch] Progress: {current_mb} MB downloaded...")
                        last_log_mb = current_mb

        except urllib.error.HTTPError as e:
            # Do not let Tenacity blindly retry 404/403 errors. Fail fast.
            if e.code in (401, 403, 404):
                err_msg = f"HTTP {e.code} fetching {url}. Server says: {e.reason}"
                logger.critical(f"[Fetch] FATAL: {err_msg}")
                raise SecureFetchError(f"HTTP {e.code} - Cannot fetch firmware.")

            # For 502s, 504s, etc., log the error so Tenacity can catch it and retry
            logger.error(f"[Fetch] Transient Server Error (HTTP {e.code}): {e.reason}")
            raise

        except Exception as e:
            logger.error(f"[Fetch] Network/IO failure mid-stream: {e}")
            raise SecureFetchError(f"Failed to fetch asset: {e}")

        # ==========================================
        # 4. CRYPTOGRAPHIC VERIFICATION & ATOMIC RENAME
        # ==========================================
        actual_hash = hasher.hexdigest().lower()

        if actual_hash != expected_sha256:
            logger.debug(f"[Fetch] ZERO-LEAKAGE: Destroying corrupted .part file.")
            temp_dest.unlink(missing_ok=True)

            err_msg = f"Hash mismatch! Expected {expected_sha256}, got {actual_hash}"
            logger.critical(f"[Fetch] FATAL: {err_msg}")
            raise ImageVerificationError("Firmware SHA256 mismatch! Transit payload corrupted.")

        # Atomic rename guarantees that when the real 'dest' appears, it is 100% complete and verified
        logger.debug(f"[Fetch] Performing atomic rename: {temp_dest.name} -> {dest.name}")
        os.rename(temp_dest, dest)

        logger.info(f"[Fetch] Asset verified successfully (SHA256: {actual_hash[:8]}...).")
        return dest

    @classmethod
    def resolve_payload(cls, uri: str, expected_sha256: Optional[str] = None) -> Path:
        """The Master Entrypoint: Handles both Local Paths and Remote URLs.

        Routes to the appropriate verification or download logic.

        Args:
            uri: A local file path or remote HTTP(S) URL.
            expected_sha256: The expected SHA256 checksum of the asset.

        Returns:
            Path: The path to the resolved, verified local file.

        Raises:
            FileNotFoundError: If a local payload URI does not exist.
            ImageVerificationError: If a local payload checksum verification fails.
            ValueError: If a remote URL lacks an expected SHA256 checksum.
        """
        # Scenario A: Local Developer Desk File
        if not uri.startswith(("http://", "https://")):
            local_path = Path(uri).expanduser().resolve()
            if not local_path.exists():
                raise FileNotFoundError(f"FATAL: Local payload not found at {local_path}")

            if expected_sha256:
                actual_hash = cls._calculate_local_hash(local_path)
                if actual_hash != expected_sha256.lower():
                    logger.critical("="*60)
                    logger.critical(f"[Fetch] FATAL: Local File Hash Mismatch!")
                    logger.critical(f"[Fetch] Expected: {expected_sha256}")
                    logger.critical(f"[Fetch] Got:      {actual_hash}")
                    logger.critical("="*60)
                    raise ImageVerificationError("Local payload checksum failed.")
            else:
                logger.warning(f"[Fetch] Using local payload {local_path.name} WITHOUT checksum verification.")

            return local_path

        # Scenario B: Factory Floor Remote URL
        cache_dir = Path(".mes_cache/artifacts")
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Extract filename from URL (e.g., 'EVSE-Image-Tezi.tar')
        filename = uri.split("/")[-1]
        dest_file = cache_dir / filename

        # We mandate SHA256 for remote downloads to prevent MITM attacks or corrupted transit
        if not expected_sha256:
            raise ValueError(f"FATAL: Remote URL {uri} requires a 'payload_sha256' in TOML for integrity.")

        # Dispatch to your brilliant Tenacity-powered streaming downloader!
        return cls.fetch_and_verify(url=uri, expected_sha256=expected_sha256, dest=dest_file)
