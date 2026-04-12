# src/pytest_mes_core/provisioning/pki.py
import hashlib
import base64
import logging
from pathlib import Path

from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.provisioning.pki")

class PKIProvisioner:
    """
    Cryptographically secure injector for End-Of-Line Certificate Provisioning.
    Defeats missing SFTP servers on raw embedded targets by using pure POSIX shell piping.
    """

    @staticmethod
    def inject_credential(
        dut_ssh: EphemeralSSHClient,
        local_filepath: Path,
        remote_dest: str,
        permissions: str = "400"
    ) -> ValidatorResult:
        """
        Pushes a local certificate/key to the DUT, enforces strict chmod permissions,
        and cryptographically verifies the transit.
        """
        if not local_filepath.exists():
            logger.error(f"[PKI] Local credential missing at {local_filepath}")
            return ValidatorResult(passed=False, error_msg="Local credential file not found.")

        # 1. Compute Local Hash
        hasher = hashlib.sha256()
        with open(local_filepath, "rb") as f:
            raw_data = f.read()
            hasher.update(raw_data)
        local_hash = hasher.hexdigest()

        logger.info(f"[PKI] Injecting {local_filepath.name} to {remote_dest} (Hash: {local_hash[:8]}...)")

        # 2. Defensive Idempotency: Check if correct file is already there
        check_cmd = f"sha256sum {remote_dest} 2>/dev/null | awk '{{print $1}}'"
        existing_hash = dut_ssh.conn.run(check_cmd, hide=True, warn=True).stdout.strip()

        if existing_hash == local_hash:
            logger.info("[PKI] Credential already exists on target with correct hash. Skipping transit.")
            # Enforce permissions even on skip
            dut_ssh.conn.run(f"chmod {permissions} {remote_dest}", hide=True, warn=True)
            return ValidatorResult(passed=True, context={"status": "already_present", "hash": local_hash})

        # 3. Base64 Encode for safe transit
        b64_payload = base64.b64encode(raw_data).decode('utf-8')

        # 4. Create remote directory tree if it doesn't exist
        remote_dir = "/".join(remote_dest.split("/")[:-1])
        dut_ssh.conn.run(f"mkdir -p {remote_dir}", hide=True, warn=True)

        # 5. Inject, Decode, and Lock Down
        inject_cmd = f"echo '{b64_payload}' | base64 -d > {remote_dest}"
        res_inject = dut_ssh.conn.run(inject_cmd, hide=True, warn=True)

        if not res_inject.ok:
            logger.error(f"[PKI] Failed to write payload to DUT: {res_inject.stderr.strip()}")
            return ValidatorResult(passed=False, error_msg="Failed to write base64 payload to DUT.")

        dut_ssh.conn.run(f"chmod {permissions} {remote_dest}", hide=True, warn=True)

        # 6. Remote Hash Verification
        remote_hash = dut_ssh.conn.run(check_cmd, hide=True, warn=True).stdout.strip()

        if local_hash != remote_hash:
            # ZERO LEAKAGE: Destroy the corrupted credential immediately
            logger.critical(f"[PKI] Transit corruption! Local: {local_hash}, Remote: {remote_hash}")
            dut_ssh.conn.run(f"rm -f {remote_dest}", hide=True, warn=True)
            return ValidatorResult(passed=False, error_msg="Cryptographic transit failure. Corrupted payload destroyed.")

        logger.info("[PKI] Injection successful and verified.")
        return ValidatorResult(passed=True, context={"hash": local_hash})

    @staticmethod
    def verify_x509_pairing(
        dut_ssh: EphemeralSSHClient,
        remote_cert_path: str,
        remote_key_path: str
    ) -> ValidatorResult:
        """
        Uses OpenSSL on the DUT to mathematically prove the public cert matches the private key.
        Prevents shipping a board that cannot authenticate to the cloud infrastructure.
        """
        logger.info(f"[PKI] Verifying cryptographic pairing of {remote_cert_path} and {remote_key_path}...")

        # Extract modulus from Certificate
        cmd_cert = f"openssl x509 -noout -modulus -in {remote_cert_path} | openssl md5"
        res_cert = dut_ssh.conn.run(cmd_cert, hide=True, warn=True)

        # Extract modulus from Private Key
        cmd_key = f"openssl rsa -noout -modulus -in {remote_key_path} | openssl md5"
        res_key = dut_ssh.conn.run(cmd_key, hide=True, warn=True)

        if not res_cert.ok or not res_key.ok:
            logger.error("[PKI] OpenSSL error on DUT. Are the files readable?")
            return ValidatorResult(passed=False, error_msg="OpenSSL missing or files unreadable on target.")

        cert_mod = res_cert.stdout.strip()
        key_mod = res_key.stdout.strip()

        if cert_mod != key_mod:
            logger.critical(f"[PKI] PAIRING FAILED! Cert Modulus: {cert_mod}, Key Modulus: {key_mod}")

            # ZERO LEAKAGE: A mismatched key pair is a fatal security liability. Destroy them to prevent shipping.
            dut_ssh.conn.run(f"rm -f {remote_cert_path} {remote_key_path}", hide=True, warn=True)
            return ValidatorResult(passed=False, error_msg="x509 Certificate and Private Key mismatch. Files destroyed.")

        logger.info("[PKI] Cryptographic pairing mathematically proven.")
        return ValidatorResult(passed=True, context={"modulus_md5": cert_mod})
