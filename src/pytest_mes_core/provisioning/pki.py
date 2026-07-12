import anyio
import functools
import structlog
import hashlib
import base64
import logging
import time
from pathlib import Path
from pytest_mes_core.transports import DutTransport
from pytest_mes_core.provisioning import ProvisioningError
from pytest_mes_core.protocols import ValidatorResult
logger = structlog.get_logger('mes_core.provisioning.pki')

class PkiProvisioner:
    """
    Cryptographically secure injector for End-Of-Line Certificate Provisioning.
    Defeats missing SFTP servers on raw embedded targets by using POSIX heredocs.
    """

    @staticmethod
    def provision_credential(transport: DutTransport, local_filepath: Path, remote_dest: str, permissions: str='400') -> None:
        """Pushes a local certificate/key to the DUT over ANY transport (SSH or Serial).

        Enforces strict chmod permissions and cryptographically verifies the transit.
        Uses POSIX heredocs and base64 chunking to bypass missing SFTP servers on
        raw embedded targets.

        Args:
            transport: The DUT transport connection.
            local_filepath: Path to the local certificate/key file.
            remote_dest: Destination path on the target device.
            permissions: POSIX permission string to apply to the remote file.

        Raises:
            ProvisioningError: If the local file is missing, chunk transmission fails,
                or cryptographic transit verification fails.
        """
        if not local_filepath.exists():
            err_msg = f'Local credential file not found at {local_filepath}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        hasher = hashlib.sha256()
        with open(local_filepath, 'rb') as f:
            raw_data = f.read()
            hasher.update(raw_data)
        local_hash = hasher.hexdigest()
        logger.info('injecting_name_to_remote_dest_sha256_val', name=local_filepath.name, remote_dest=remote_dest, val=local_hash[:8])
        check_cmd = f"sha256sum {remote_dest} 2>/dev/null | awk '{{print $1}}'"
        logger.debug('executing_idempotency_check_check_cmd', check_cmd=check_cmd)
        res_check = transport.safe_run(check_cmd)
        if res_check.exited == 0 and res_check.stdout.strip() == local_hash:
            logger.info('[PKI] Credential already exists on target with correct hash. Skipping transit.')
            logger.debug('enforcing_strict_permissions_chmod_permissions_remote_dest', permissions=permissions, remote_dest=remote_dest)
            transport.safe_run(f'chmod {permissions} {remote_dest}')
            return
        b64_string = base64.b64encode(raw_data).decode('utf-8')
        b64_chunks = [b64_string[i:i + 64] for i in range(0, len(b64_string), 64)]
        logger.debug('base64_payload_formatted_val_lines_val_1_bytes', val=len(b64_chunks), val_1=len(b64_string))
        remote_dir = '/'.join(remote_dest.split('/')[:-1])
        logger.debug('building_remote_directory_tree_mkdir_p_remote_dir', remote_dir=remote_dir)
        transport.safe_run(f'mkdir -p {remote_dir}')
        logger.debug('transmitting_payload_iteratively_secrets_masked_in_trace')
        b64_temp = f'{remote_dest}.b64'
        transport.safe_run(f'> {b64_temp}')
        for chunk in b64_chunks:
            res_chunk = transport.safe_run(f"echo '{chunk}' >> {b64_temp}", sensitive=True)
            if res_chunk.exited != 0:
                err_msg = f'Failed to write payload chunk to DUT: {res_chunk.stderr.strip()}'
                logger.critical('fatal_err_msg', err_msg=err_msg)
                raise ProvisioningError(err_msg)
            time.sleep(0.05)
        res_decode = transport.safe_run(f'base64 -d {b64_temp} > {remote_dest}')
        transport.safe_run(f'rm -f {b64_temp}')
        if res_decode.exited != 0:
            err_msg = f'Failed to decode base64 payload on DUT: {res_decode.stderr.strip()}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        logger.debug('enforcing_strict_permissions_chmod_permissions_remote_dest', permissions=permissions, remote_dest=remote_dest)
        transport.safe_run(f'chmod {permissions} {remote_dest}')
        logger.debug('[PKI] Requesting remote SHA256 verification hash...')
        remote_hash = transport.safe_run(check_cmd).stdout.strip()
        logger.debug('remote_hash_computed_remote_hash', remote_hash=remote_hash)
        if local_hash != remote_hash:
            logger.critical('fatal_transit_corruption_local_local_hash_remote_remote_hash', local_hash=local_hash, remote_hash=remote_hash)
            logger.debug('zero_leakage_destroying_corrupted_payload_rm_f_remote_dest', remote_dest=remote_dest)
            transport.safe_run(f'rm -f {remote_dest}')
            raise ProvisioningError('Cryptographic transit failure. Corrupted payload destroyed on target.')
        logger.info('injection_successful_and_cryptographically_verified_remote_dest', remote_dest=remote_dest)

    @staticmethod
    async def async_provision_credential(transport: DutTransport, local_filepath: Path, remote_dest: str, permissions: str = '400') -> None:
        return await anyio.to_thread.run_sync(
            functools.partial(PkiProvisioner.provision_credential, transport, local_filepath, remote_dest, permissions))

class PkiPairingValidator:
    """
    Validation Protocol to mathematically prove the public cert matches the private key.
    Belongs in the Test Phase to prevent shipping unauthenticated boards.
    """

    @staticmethod
    def verify_x509_pairing(transport: DutTransport, remote_cert_path: str, remote_key_path: str) -> ValidatorResult:
        """Verifies cryptographic pairing of a certificate and private key on the target.

        Args:
            transport: The DUT transport connection.
            remote_cert_path: Path to the certificate file on the target.
            remote_key_path: Path to the private key file on the target.

        Returns:
            ValidatorResult: Contains success status and any relevant error messages.
        """
        logger.info('verifying_cryptographic_pairing_of_remote_cert_path_and_remote_key_path', remote_cert_path=remote_cert_path, remote_key_path=remote_key_path)
        cmd_cert = f'openssl x509 -noout -modulus -in {remote_cert_path} | openssl md5'
        logger.debug('extracting_cert_modulus_cmd_cert', cmd_cert=cmd_cert)
        res_cert = transport.safe_run(cmd_cert)
        cmd_key = f'openssl rsa -noout -modulus -in {remote_key_path} | openssl md5'
        logger.debug('extracting_key_modulus_cmd_key', cmd_key=cmd_key)
        res_key = transport.safe_run(cmd_key)
        if res_cert.exited != 0 or res_key.exited != 0:
            logger.error('openssl_error_on_dut_cert_exit_exited_key_exit_exited_1', exited=res_cert.exited, exited_1=res_key.exited)
            if res_cert.stderr:
                logger.debug('cert_stderr_val', val=res_cert.stderr.strip())
            if res_key.stderr:
                logger.debug('key_stderr_val', val=res_key.stderr.strip())
            return ValidatorResult(passed=False, error_msg='OpenSSL missing or files unreadable on target.')
        cert_mod = res_cert.stdout.strip()
        key_mod = res_key.stdout.strip()
        logger.debug('certificate_md5_modulus_cert_mod', cert_mod=cert_mod)
        logger.debug('private_key_md5_modulus_key_mod', key_mod=key_mod)
        if cert_mod != key_mod:
            logger.critical('fatal_pairing_failed_cert_modulus_cert_mod_key_modulus_key_mod', cert_mod=cert_mod, key_mod=key_mod)
            logger.debug('zero_leakage_destroying_mismatched_key_pair_rm_f_remote_cert_path_remote_key_path', remote_cert_path=remote_cert_path, remote_key_path=remote_key_path)
            transport.safe_run(f'rm -f {remote_cert_path} {remote_key_path}')
            return ValidatorResult(passed=False, error_msg='x509 Certificate and Private Key mismatch. Files destroyed.')
        logger.info('[PKI] Cryptographic pairing mathematically proven.')
        return ValidatorResult(passed=True)
    @staticmethod
    async def async_verify_x509_pairing(transport: DutTransport, remote_cert_path: str, remote_key_path: str) -> ValidatorResult:
        return await anyio.to_thread.run_sync(
            functools.partial(PkiPairingValidator.verify_x509_pairing, transport, remote_cert_path, remote_key_path))
