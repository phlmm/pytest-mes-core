import pytest
import base64
from unittest.mock import MagicMock, patch
from pathlib import Path
from pytest_mes_core.provisioning.pki import PkiProvisioner, PkiPairingValidator
from pytest_mes_core.provisioning import ProvisioningError
from pytest_mes_core.transports.base import CommandResult

def test_pki_provisioning_idempotency(tmp_path):
    local_key = tmp_path / 'key.pem'
    local_key.write_bytes(b'mock_private_key_data')
    import hashlib
    hasher = hashlib.sha256()
    hasher.update(b'mock_private_key_data')
    expected_hash = hasher.hexdigest()
    mock_transport = MagicMock()
    mock_transport.safe_run.return_value = CommandResult(command='sha', ok=True, exited=0, stdout=expected_hash + '\n', stderr='', duration_s=0.1)
    PkiProvisioner.provision_credential(transport=mock_transport, local_filepath=local_key, remote_dest='/etc/ssl/key.pem')
    mock_transport.safe_run.assert_any_call("sha256sum /etc/ssl/key.pem 2>/dev/null | awk '{print $1}'")
    mock_transport.safe_run.assert_any_call('chmod 400 /etc/ssl/key.pem')
    for call in mock_transport.safe_run.call_args_list:
        assert 'base64' not in call[0][0]

def test_pki_provisioning_transit_corruption_triggers_zero_leakage(tmp_path):
    local_key = tmp_path / 'key.pem'
    local_key.write_bytes(b'mock_private_key_data')
    mock_transport = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'sha256sum' in cmd:
            if mock_transport.safe_run.call_count == 1:
                return CommandResult(command=cmd, ok=False, exited=1, stdout='', stderr='', duration_s=0.1)
            else:
                return CommandResult(command=cmd, ok=True, exited=0, stdout='corrupted_hash', stderr='', duration_s=0.1)
        elif 'echo' in cmd or 'base64' in cmd or 'mkdir' in cmd or ('chmod' in cmd):
            return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_transport.safe_run.side_effect = safe_run_side_effect
    with pytest.raises(ProvisioningError, match='Cryptographic transit failure'):
        PkiProvisioner.provision_credential(transport=mock_transport, local_filepath=local_key, remote_dest='/etc/ssl/key.pem')
    mock_transport.safe_run.assert_any_call('rm -f /etc/ssl/key.pem')

def test_pki_provision_credential_marks_only_chunk_writes_as_sensitive(tmp_path):
    """Fix 6: base64 chunk `echo` writes must carry sensitive=True (they
    contain private-key material); mkdir/chmod/decode/hash calls must not."""
    local_key = tmp_path / 'key.pem'
    local_key.write_bytes(b'mock_private_key_data' * 10)
    import hashlib
    hasher = hashlib.sha256()
    hasher.update(local_key.read_bytes())
    expected_hash = hasher.hexdigest()
    mock_transport = MagicMock()
    sha_call_count = 0

    def safe_run_side_effect(cmd, *args, **kwargs):
        nonlocal sha_call_count
        if 'sha256sum' in cmd:
            sha_call_count += 1
            if sha_call_count == 1:
                return CommandResult(command=cmd, ok=False, exited=1, stdout='', stderr='', duration_s=0.1)
            return CommandResult(command=cmd, ok=True, exited=0, stdout=expected_hash, stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_transport.safe_run.side_effect = safe_run_side_effect
    PkiProvisioner.provision_credential(transport=mock_transport, local_filepath=local_key, remote_dest='/etc/ssl/key.pem')
    chunk_calls = [c for c in mock_transport.safe_run.call_args_list if c.args[0].startswith("echo '")]
    assert len(chunk_calls) > 1
    for c in chunk_calls:
        assert c.kwargs.get('sensitive') is True
    non_chunk_calls = [c for c in mock_transport.safe_run.call_args_list if not c.args[0].startswith("echo '")]
    assert len(non_chunk_calls) > 0
    for c in non_chunk_calls:
        assert 'sensitive' not in c.kwargs

def test_pki_validator_destroys_mismatched_keys():
    mock_transport = MagicMock()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if 'x509' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='MODULUS_CERT', stderr='', duration_s=0.1)
        elif 'rsa' in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout='MODULUS_KEY_MISMATCH', stderr='', duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout='', stderr='', duration_s=0.1)
    mock_transport.safe_run.side_effect = safe_run_side_effect
    res = PkiPairingValidator.verify_x509_pairing(mock_transport, '/cert.pem', '/key.pem')
    assert not res.passed
    assert 'mismatch' in res.error_msg
    mock_transport.safe_run.assert_any_call('rm -f /cert.pem /key.pem')