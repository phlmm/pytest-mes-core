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
    firmware = tmp_path / 'fw.bin'
    expected = bytes(range(256)) * 1
    firmware.write_bytes(expected)
    stub = _SyncSwdStub(expected[:128])
    provisioner = McuProvisioner(stub)
    with pytest.raises(ImageVerificationError, match='length mismatch'):
        provisioner._verify_flash(firmware, base_address=134217728)