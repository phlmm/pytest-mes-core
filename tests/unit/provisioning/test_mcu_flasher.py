import pytest

from pytest_mes_core.provisioning.mcu_flasher import McuProvisioner
from pytest_mes_core.provisioning.base import ImageVerificationError


class _SyncSwdStub:
    """Minimal swd transport stub exposing only read_memory."""

    def __init__(self, readback: bytes):
        self._readback = readback

    def read_memory(self, address, size):
        return self._readback


def test_verify_flash_raises_on_truncated_readback(tmp_path):
    firmware = tmp_path / "fw.bin"
    expected = bytes(range(256)) * 1  # 256 bytes, values 0..255
    firmware.write_bytes(expected)

    # Short SWD read: only the first 128 bytes come back, but they are a
    # byte-for-byte prefix match of `expected` -- zip() alone would find no
    # differing byte and silently "pass" verification.
    stub = _SyncSwdStub(expected[:128])
    provisioner = McuProvisioner(stub)

    with pytest.raises(ImageVerificationError, match="length mismatch"):
        provisioner._verify_flash(firmware, base_address=0x0800_0000)


class _AsyncSwdStub:
    """Async swd transport stub exposing async_download + async_read_memory."""

    def __init__(self, read_result=None, read_exc=None):
        self._read_result = read_result
        self._read_exc = read_exc
        self.download_calls = []
        self.read_calls = []

    async def async_download(self, path, **kwargs):
        self.download_calls.append((path, kwargs))

    async def async_read_memory(self, address, size):
        self.read_calls.append((address, size))
        if self._read_exc is not None:
            raise self._read_exc
        return self._read_result

    async def async_reset(self):
        pass


@pytest.mark.anyio
async def test_async_flash_firmware_skips_verify_for_hex(tmp_path):
    firmware = tmp_path / "fw.hex"
    firmware.write_text(":00000001FF\n")

    stub = _AsyncSwdStub(read_result=b"unused")
    provisioner = McuProvisioner(stub)

    result = await provisioner.async_flash_firmware(firmware, verify=True)

    assert result is True
    assert stub.read_calls == []  # async_read_memory never called for .hex


@pytest.mark.anyio
async def test_async_flash_firmware_bin_verify_error_is_non_fatal(tmp_path):
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"\x01\x02\x03\x04")

    stub = _AsyncSwdStub(read_exc=RuntimeError("transient SWD hiccup"))
    provisioner = McuProvisioner(stub)

    # Should not raise -- a transient read error is a warning, not fatal.
    result = await provisioner.async_flash_firmware(firmware, verify=True)
    assert result is True
    assert len(stub.read_calls) == 1


@pytest.mark.anyio
async def test_async_verify_flash_length_mismatch_raises(tmp_path):
    # Exercises async_verify_flash directly (mirrors the sync _verify_flash unit
    # test above): async_flash_firmware's outer try/except re-wraps *any*
    # exception (including ProvisioningError subclasses) into a plain
    # ProvisioningError, so the undecorated ImageVerificationError type is only
    # observable at the async_verify_flash boundary itself.
    firmware = tmp_path / "fw.bin"
    expected = bytes(range(64))
    firmware.write_bytes(expected)

    stub = _AsyncSwdStub(read_result=expected[:32])  # short, prefix-equal
    provisioner = McuProvisioner(stub)

    with pytest.raises(ImageVerificationError, match="length mismatch"):
        await provisioner.async_verify_flash(firmware, base_address=0x0800_0000)


@pytest.fixture
def anyio_backend():
    return "asyncio"
