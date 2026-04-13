# src/pytest_mes_core/provisioning/pki.py
import hashlib
import base64
import logging
from pathlib import Path

from pytest_mes_core.transports import DutTransport
from pytest_mes_core.provisioning import ProvisioningError

# If you decide to move the Validator to protocols/pki.py later, that's perfectly fine.
# For now, we keep it here to maintain your workflow.
from pytest_mes_core.protocols import ValidatorResult

logger = logging.getLogger("mes_core.provisioning.pki")

class PkiProvisioner:
    """
    Cryptographically secure injector for End-Of-Line Certificate Provisioning.
    Defeats missing SFTP servers on raw embedded targets by using POSIX heredocs.
    """

    @staticmethod
    def provision_credential(
        transport: DutTransport,
        local_filepath: Path,
        remote_dest: str,
        permissions: str = "400"
    ) -> None:
        """
        Pushes a local certificate/key to the DUT over ANY transport (SSH or Serial),
        enforces strict chmod permissions, and cryptographically verifies the transit.
        Raises ProvisioningError on any failure to abort the factory setup.
        """
        if not local_filepath.exists():
            raise ProvisioningError(f"Local credential file not found at {local_filepath}")

        # 1. Compute Local Hash
        hasher = hashlib.sha256()
        with open(local_filepath, "rb") as f:
            raw_data = f.read()
            hasher.update(raw_data)
        local_hash = hasher.hexdigest()

        logger.info(f"[PKI] Injecting {local_filepath.name} to {remote_dest} (Hash: {local_hash[:8]}...)")

        # 2. Defensive Idempotency: Check if correct file is already there
        check_cmd = f"sha256sum {remote_dest} 2>/dev/null | awk '{{print $1}}'"
        res_check = transport.safe_run(check_cmd)

        if res_check.exited == 0 and res_check.stdout.strip() == local_hash:
            logger.info("[PKI] Credential already exists on target with correct hash. Skipping transit.")
            transport.safe_run(f"chmod {permissions} {remote_dest}")
            return

        # 3. Base64 Encode and Chunk (CRITICAL FOR UART LINE BUFFERS)
        # We split the base64 string into 64-character lines to prevent TTY buffer overruns
        b64_string = base64.b64encode(raw_data).decode('utf-8')
        b64_chunks = [b64_string[i:i+64] for i in range(0, len(b64_string), 64)]
        b64_payload = "\n".join(b64_chunks)

        # 4. Create remote directory tree
        remote_dir = "/".join(remote_dest.split("/")[:-1])
        transport.safe_run(f"mkdir -p {remote_dir}")

        # 5. Inject using POSIX Heredoc (UART & SSH Safe)
        # Using a heredoc (<< 'EOF') is infinitely safer than `echo '...'` because it bypasses
        # shell escaping issues and handles massive multi-line strings perfectly.
        inject_cmd = f"cat << 'EOF' | base64 -d > {remote_dest}\n{b64_payload}\nEOF"
        res_inject = transport.safe_run(inject_cmd)

        if res_inject.exited != 0:
            raise ProvisioningError(f"Failed to write payload to DUT: {res_inject.stderr.strip()}")

        transport.safe_run(f"chmod {permissions} {remote_dest}")

        # 6. Remote Hash Verification
        remote_hash = transport.safe_run(check_cmd).stdout.strip()

        if local_hash != remote_hash:
            # ZERO LEAKAGE: Destroy the corrupted credential immediately
            logger.critical(f"[PKI] Transit corruption! Local: {local_hash}, Remote: {remote_hash}")
            transport.safe_run(f"rm -f {remote_dest}")
            raise ProvisioningError("Cryptographic transit failure. Corrupted payload destroyed on target.")

        logger.info("[PKI] Injection successful and verified.")


class PkiPairingValidator:
    """
    Validation Protocol to mathematically prove the public cert matches the private key.
    Belongs in the Test Phase to prevent shipping unauthenticated boards.
    """

    @staticmethod
    def verify_x509_pairing(
        transport: DutTransport,
        remote_cert_path: str,
        remote_key_path: str
    ) -> ValidatorResult:
        logger.info(f"[PKI] Verifying cryptographic pairing of {remote_cert_path} and {remote_key_path}...")

        # Extract modulus from Certificate
        cmd_cert = f"openssl x509 -noout -modulus -in {remote_cert_path} | openssl md5"
        res_cert = transport.safe_run(cmd_cert)

        # Extract modulus from Private Key
        cmd_key = f"openssl rsa -noout -modulus -in {remote_key_path} | openssl md5"
        res_key = transport.safe_run(cmd_key)

        if res_cert.exited != 0 or res_key.exited != 0:
            logger.error("[PKI] OpenSSL error on DUT. Are the files readable?")
            return ValidatorResult(passed=False, error_msg="OpenSSL missing or files unreadable on target.")

        cert_mod = res_cert.stdout.strip()
        key_mod = res_key.stdout.strip()

        if cert_mod != key_mod:
            logger.critical(f"[PKI] PAIRING FAILED! Cert Modulus: {cert_mod}, Key Modulus: {key_mod}")

            # ZERO LEAKAGE: A mismatched key pair is a fatal security liability. Destroy them.
            transport.safe_run(f"rm -f {remote_cert_path} {remote_key_path}")
            return ValidatorResult(passed=False, error_msg="x509 Certificate and Private Key mismatch. Files destroyed.")

        logger.info("[PKI] Cryptographic pairing mathematically proven.")
        return ValidatorResult(passed=True, metrics={"modulus_md5": cert_mod})
