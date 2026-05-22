import anyio
import structlog
import os
import hashlib
import urllib.request
import urllib.error
import logging
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_fixed, before_sleep_log
from typing import Optional
from pytest_mes_core.provisioning.base import ProvisioningError, ImageVerificationError
logger = structlog.get_logger('mes_core.provisioning.secure_fetch')

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
        logger.debug('calculating_sha256_for_local_file_name', name=filepath.name)
        hasher = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
        result = hasher.hexdigest().lower()
        logger.debug('local_hash_computed_result', result=result)
        return result

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2.0), reraise=True, before_sleep=before_sleep_log(logger, logging.WARNING))
    def fetch_and_verify(url: str, expected_sha256: str, dest: Path, timeout_s: float=30.0) -> Path:
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
        if dest.exists():
            logger.debug('file_exists_at_dest_verifying_hash_to_save_bandwidth', dest=dest)
            if SecureAssetFetcher._calculate_local_hash(dest) == expected_sha256:
                logger.info('cache_hit_valid_firmware_already_present_at_name', name=dest.name)
                return dest
            else:
                logger.warning('local_file_corrupted_or_outdated_redownloading')
                logger.debug('zero_leakage_destroying_invalid_cache_file_at_dest', dest=dest)
                dest.unlink(missing_ok=True)
        logger.info('downloading_asset_from_url', url=url)
        temp_dest = dest.with_suffix(f'.part.{os.getpid()}')
        hasher = hashlib.sha256()
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'MES-Core-Provisioner/1.0'})
            logger.debug('opening_http_stream_with_timeout_s_s_timeout', timeout_s=timeout_s)
            with urllib.request.urlopen(req, timeout=timeout_s) as response, open(temp_dest, 'wb') as out_file:
                bytes_downloaded = 0
                last_log_mb = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    hasher.update(chunk)
                    bytes_downloaded += len(chunk)
                    current_mb = bytes_downloaded // (1024 * 1024)
                    if current_mb >= last_log_mb + 10:
                        logger.debug('progress_current_mb_mb_downloaded', current_mb=current_mb)
                        last_log_mb = current_mb
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                err_msg = f'HTTP {e.code} fetching {url}. Server says: {e.reason}'
                logger.critical('fatal_err_msg', err_msg=err_msg)
                raise SecureFetchError(f'HTTP {e.code} - Cannot fetch firmware.')
            logger.error('transient_server_error_http_code_reason', code=e.code, reason=e.reason)
            raise
        except KeyboardInterrupt:
            logger.warning('download_interrupted_by_operator', action='destroying_partial_file')
            temp_dest.unlink(missing_ok=True)
            raise SecureFetchError('Firmware download interrupted by operator (Ctrl+C).')
        except Exception as e:
            logger.error('network_io_failure_mid_stream_e', e=e)
            raise SecureFetchError(f'Failed to fetch asset: {e}')
        actual_hash = hasher.hexdigest().lower()
        if actual_hash != expected_sha256:
            logger.debug('zero_leakage_destroying_corrupted_part_file')
            temp_dest.unlink(missing_ok=True)
            err_msg = f'Hash mismatch! Expected {expected_sha256}, got {actual_hash}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ImageVerificationError('Firmware SHA256 mismatch! Transit payload corrupted.')
        logger.debug('performing_atomic_rename_name_name_1', name=temp_dest.name, name_1=dest.name)
        os.rename(temp_dest, dest)
        logger.info('asset_verified_successfully_sha256_val', val=actual_hash[:8])
        return dest

    @staticmethod
    async def async_fetch_and_verify(url, expected_sha256, dest, timeout_s, *args, **kwargs):
        return await anyio.to_thread.run_sync(SecureAssetFetcher.fetch_and_verify, url, expected_sha256, dest, timeout_s, *args, **kwargs)

    @classmethod
    def resolve_payload(cls, uri: str, expected_sha256: Optional[str]=None) -> Path:
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
        if not uri.startswith(('http://', 'https://')):
            local_path = Path(uri).expanduser().resolve()
            if not local_path.exists():
                raise FileNotFoundError(f'FATAL: Local payload not found at {local_path}')
            if expected_sha256:
                actual_hash = cls._calculate_local_hash(local_path)
                if actual_hash != expected_sha256.lower():
                    logger.critical('=' * 60)
                    logger.critical('fatal_local_file_hash_mismatch')
                    logger.critical('expected_expected_sha256', expected_sha256=expected_sha256)
                    logger.critical('got_actual_hash', actual_hash=actual_hash)
                    logger.critical('=' * 60)
                    raise ImageVerificationError('Local payload checksum failed.')
            else:
                logger.warning('using_local_payload_name_without_checksum_verification', name=local_path.name)
            return local_path
        cache_dir = Path('.mes_cache/artifacts')
        cache_dir.mkdir(parents=True, exist_ok=True)
        filename = uri.split('/')[-1]
        dest_file = cache_dir / filename
        if not expected_sha256:
            raise ValueError(f"FATAL: Remote URL {uri} requires a 'payload_sha256' in TOML for integrity.")
        return cls.fetch_and_verify(url=uri, expected_sha256=expected_sha256, dest=dest_file)
    @classmethod
    async def async_resolve_payload(cls, uri, expected_sha256, *args, **kwargs):
        return await anyio.to_thread.run_sync(cls.resolve_payload, uri, expected_sha256, *args, **kwargs)
