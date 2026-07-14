"""
pytest_mes_core.utils.device_crypto
====================================

AES-128-CBC + HMAC-SHA256 envelope for encrypted MQTT payloads.

Many MCU projects encrypt their MQTT telemetry to prevent spoofing on
shared broker infrastructure.  This module provides a Python
implementation that mirrors the typical embedded C pattern::

    { "iv": "<base64>", "ciphertext": "<base64>", "hmac": "<base64>" }

Usage::

    from pytest_mes_core.utils.device_crypto import DeviceCrypto

    # With a project-specific key
    crypto = DeviceCrypto(key=bytes.fromhex("A1B2C3D4E5F67890123456789012ABCD"))

    encrypted = crypto.encrypt("hello world")
    plaintext = crypto.decrypt(encrypted)

The key can be loaded from ``station_env.toml`` or passed directly.

Only genuinely non-envelope input (invalid JSON, non-dict, or missing
``iv``/``ciphertext``/``hmac`` keys) is passed through unchanged. A
well-formed envelope that fails HMAC verification/decryption under both
the primary and fallback keys is a tampered or corrupted message -- it
raises :class:`DeviceCryptoError` instead of being silently returned as
raw envelope JSON.
"""

from __future__ import annotations

import base64
import json
import os

import structlog
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hmac import HMAC

logger = structlog.get_logger("mes_core.utils.device_crypto")


class DeviceCryptoError(Exception):
    """Raised when a well-formed envelope fails HMAC verification/decryption
    under every configured key -- i.e. a tampered or corrupted message."""


class DeviceCrypto:
    """AES-128-CBC + HMAC-SHA256 encrypt/decrypt envelope.

    Parameters
    ----------
    key:
        16-byte AES-128 key used for both encryption and HMAC.
    fallback_key:
        Optional second key to try during decryption (for key rotation).
    """

    def __init__(self, key: bytes, fallback_key: bytes | None = None) -> None:
        if len(key) != 16:
            raise ValueError(f"AES-128 key must be exactly 16 bytes, got {len(key)}")
        self._key = key
        self._fallback_key = fallback_key

    def encrypt(self, plaintext_str: str) -> str:
        """Encrypt a plaintext string into a JSON envelope.

        Returns
        -------
        str
            JSON string with ``iv``, ``ciphertext``, and ``hmac`` fields.
        """
        plaintext = plaintext_str.encode("utf-8")

        # 1. PKCS7 padding
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(plaintext) + padder.finalize()

        # 2. Generate random IV
        iv = os.urandom(16)

        # 3. AES-128-CBC encrypt
        cipher = Cipher(algorithms.AES(self._key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()

        # 4. HMAC-SHA256 over IV || ciphertext
        h = HMAC(self._key, hashes.SHA256())
        h.update(iv + ciphertext)
        mac = h.finalize()

        # 5. JSON envelope
        envelope = {
            "iv": base64.b64encode(iv).decode("utf-8"),
            "ciphertext": base64.b64encode(ciphertext).decode("utf-8"),
            "hmac": base64.b64encode(mac).decode("utf-8"),
        }
        return json.dumps(envelope, separators=(",", ":"))

    def decrypt(self, envelope_json: str) -> str:
        """Decrypt a JSON envelope back to plaintext.

        If decryption with the primary key fails and a ``fallback_key``
        was provided, retries with the fallback (key rotation support).

        If the input is not a valid envelope at all (invalid JSON, not a
        JSON object, or missing ``iv``/``ciphertext``/``hmac`` keys), it is
        returned unchanged -- this allows transparent pass-through of
        unencrypted messages.

        If the input IS a well-formed envelope but HMAC verification or
        decryption fails under every configured key, the message is a
        tampered or corrupted envelope, not a plain-text pass-through --
        this raises :class:`DeviceCryptoError` instead of returning the
        raw envelope JSON.

        Returns
        -------
        str
            The decrypted plaintext, or the original input if not an envelope.

        Raises
        ------
        DeviceCryptoError
            If the input is a well-formed envelope but fails to verify/decrypt
            with all configured keys.
        """
        try:
            data = json.loads(envelope_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            return envelope_json
        if not isinstance(data, dict) or not all(
            k in data for k in ("iv", "ciphertext", "hmac")
        ):
            return envelope_json

        try:
            return self._try_decrypt(data, self._key)
        except Exception as primary_exc:
            if self._fallback_key:
                try:
                    logger.debug("crypto_trying_fallback_key")
                    return self._try_decrypt(data, self._fallback_key)
                except Exception:
                    pass
            logger.warning(
                "crypto_envelope_verification_failed",
                error=str(primary_exc),
            )
            raise DeviceCryptoError(
                "Envelope HMAC verification/decryption failed with all configured keys"
            ) from primary_exc

    @staticmethod
    def _try_decrypt(data: dict, key: bytes) -> str:
        iv = base64.b64decode(data["iv"])
        ciphertext = base64.b64decode(data["ciphertext"])
        mac_received = base64.b64decode(data["hmac"])

        # 1. Verify HMAC
        h = HMAC(key, hashes.SHA256())
        h.update(iv + ciphertext)
        h.verify(mac_received)

        # 2. Decrypt
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        # 3. Remove PKCS7 padding
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded_plaintext) + unpadder.finalize()

        return plaintext.decode("utf-8")
