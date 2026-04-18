import pytest
import os
from unittest.mock import patch, MagicMock
from pytest_mes_core.provisioning.secure_fetch import SecureAssetFetcher, ImageVerificationError

def test_secure_fetch_idempotency_cache_hit(tmp_path):
    # Pre-create a valid file
    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"hello world")

    import hashlib
    hasher = hashlib.sha256()
    hasher.update(b"hello world")
    expected_hash = hasher.hexdigest()

    # The fetcher should immediately return without opening streams
    result = SecureAssetFetcher.fetch_and_verify(
        url="http://fake.url/firmware.bin",
        expected_sha256=expected_hash,
        dest=firmware_path
    )

    assert result == firmware_path

@patch("pytest_mes_core.provisioning.secure_fetch.urllib.request.urlopen")
def test_secure_fetch_atomic_part_file(mock_urlopen, tmp_path):
    import io
    def urlopen_side_effect(*args, **kwargs):
        mock_response = MagicMock()
        mock_response.read = io.BytesIO(b"mock_data").read
        mock_response.__enter__.return_value = mock_response
        return mock_response

    mock_urlopen.side_effect = urlopen_side_effect

    import hashlib
    hasher = hashlib.sha256()
    hasher.update(b"mock_data")
    expected_hash = hasher.hexdigest()

    dest_path = tmp_path / "firmware.bin"

    result = SecureAssetFetcher.fetch_and_verify(
        url="http://fake.url/firmware.bin",
        expected_sha256=expected_hash,
        dest=dest_path
    )

    assert result == dest_path
    assert dest_path.exists()
    assert dest_path.read_bytes() == b"mock_data"

    # Part file should be destroyed via rename
    part_files = list(tmp_path.glob("*.part*"))
    assert len(part_files) == 0

@patch("pytest_mes_core.provisioning.secure_fetch.urllib.request.urlopen")
def test_secure_fetch_corruption_triggers_zero_leakage(mock_urlopen, tmp_path):
    import io
    def urlopen_side_effect(*args, **kwargs):
        mock_response = MagicMock()
        mock_response.read = io.BytesIO(b"corrupted_data").read
        mock_response.__enter__.return_value = mock_response
        return mock_response

    mock_urlopen.side_effect = urlopen_side_effect

    dest_path = tmp_path / "firmware.bin"

    with pytest.raises(ImageVerificationError, match="Firmware SHA256 mismatch"):
        SecureAssetFetcher.fetch_and_verify(
            url="http://fake.url/firmware.bin",
            expected_sha256="0000000000000000000000000000000000000000000000000000000000000000",
            dest=dest_path
        )

    # Zero leakage: .part file must be destroyed
    part_files = list(tmp_path.glob("*.part*"))
    assert len(part_files) == 0
    # Final file must not exist
    assert not dest_path.exists()
