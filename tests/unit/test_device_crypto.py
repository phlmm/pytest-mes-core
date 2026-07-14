import base64
import json

import pytest

from pytest_mes_core.utils.device_crypto import DeviceCrypto, DeviceCryptoError


KEY = bytes.fromhex("A1B2C3D4E5F67890123456789012ABCD")
FALLBACK_KEY = bytes.fromhex("000102030405060708090A0B0C0D0E0F")


def test_round_trip():
    crypto = DeviceCrypto(key=KEY)
    envelope = crypto.encrypt("hello world")
    assert crypto.decrypt(envelope) == "hello world"


def test_fallback_key_rotation():
    # Encrypted with the "old" key, decrypted by a crypto instance whose
    # primary key is the "new" one but which still carries the old key
    # as fallback_key.
    old_crypto = DeviceCrypto(key=FALLBACK_KEY)
    envelope = old_crypto.encrypt("rotate me")

    new_crypto = DeviceCrypto(key=KEY, fallback_key=FALLBACK_KEY)
    assert new_crypto.decrypt(envelope) == "rotate me"


def test_non_envelope_string_passes_through():
    crypto = DeviceCrypto(key=KEY)
    assert crypto.decrypt("just a plain log line") == "just a plain log line"
    assert crypto.decrypt("{}") == "{}"
    assert crypto.decrypt(json.dumps({"foo": "bar"})) == json.dumps({"foo": "bar"})
    assert crypto.decrypt("[1, 2, 3]") == "[1, 2, 3]"


def test_tampered_ciphertext_raises_device_crypto_error():
    crypto = DeviceCrypto(key=KEY)
    envelope = crypto.encrypt("sensitive telemetry")
    data = json.loads(envelope)

    ciphertext = bytearray(base64.b64decode(data["ciphertext"]))
    ciphertext[0] ^= 0xFF  # flip one byte
    data["ciphertext"] = base64.b64encode(bytes(ciphertext)).decode("utf-8")
    tampered_envelope = json.dumps(data)

    with pytest.raises(DeviceCryptoError):
        crypto.decrypt(tampered_envelope)


def test_tampered_envelope_raises_even_with_fallback_key_configured():
    crypto = DeviceCrypto(key=KEY, fallback_key=FALLBACK_KEY)
    envelope = crypto.encrypt("sensitive telemetry")
    data = json.loads(envelope)

    hmac_bytes = bytearray(base64.b64decode(data["hmac"]))
    hmac_bytes[0] ^= 0xFF
    data["hmac"] = base64.b64encode(bytes(hmac_bytes)).decode("utf-8")
    tampered_envelope = json.dumps(data)

    with pytest.raises(DeviceCryptoError):
        crypto.decrypt(tampered_envelope)


def test_15_byte_key_rejected_at_construction():
    with pytest.raises(ValueError, match="16 bytes"):
        DeviceCrypto(key=bytes(15))
