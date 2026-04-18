import pytest
import base64
from unittest.mock import MagicMock, patch
from pathlib import Path
from pytest_mes_core.provisioning.pki import PkiProvisioner, PkiPairingValidator
from pytest_mes_core.provisioning import ProvisioningError
from pytest_mes_core.transports.base import CommandResult

def test_pki_provisioning_idempotency(tmp_path):
    # 1. Setup mock files and transport
    local_key = tmp_path / "key.pem"
    local_key.write_bytes(b"mock_private_key_data")
    
    # Pre-calculate what the hash should be
    import hashlib
    hasher = hashlib.sha256()
    hasher.update(b"mock_private_key_data")
    expected_hash = hasher.hexdigest()

    mock_transport = MagicMock()
    # Mock safe_run to return the exact hash on the first check_cmd
    mock_transport.safe_run.return_value = CommandResult(command="sha", ok=True, exited=0, stdout=expected_hash + "\n", stderr="", duration_s=0.1)

    PkiProvisioner.provision_credential(
        transport=mock_transport,
        local_filepath=local_key,
        remote_dest="/etc/ssl/key.pem"
    )

    # 2. Verify idempotency
    # It should call sha256sum first
    mock_transport.safe_run.assert_any_call("sha256sum /etc/ssl/key.pem 2>/dev/null | awk '{print $1}'")
    
    # It should immediately chmod and return
    mock_transport.safe_run.assert_any_call("chmod 400 /etc/ssl/key.pem")
    
    # It should NOT call base64 commands
    for call in mock_transport.safe_run.call_args_list:
        assert "base64" not in call[0][0]

def test_pki_provisioning_transit_corruption_triggers_zero_leakage(tmp_path):
    local_key = tmp_path / "key.pem"
    local_key.write_bytes(b"mock_private_key_data")

    mock_transport = MagicMock()
    
    def safe_run_side_effect(cmd, *args, **kwargs):
        if "sha256sum" in cmd:
            # Return empty hash on first check, but corrupt hash on validation check
            if mock_transport.safe_run.call_count == 1:
                return CommandResult(command=cmd, ok=False, exited=1, stdout="", stderr="", duration_s=0.1)
            else:
                return CommandResult(command=cmd, ok=True, exited=0, stdout="corrupted_hash", stderr="", duration_s=0.1)
        elif "echo" in cmd or "base64" in cmd or "mkdir" in cmd or "chmod" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_transport.safe_run.side_effect = safe_run_side_effect

    with pytest.raises(ProvisioningError, match="Cryptographic transit failure"):
        PkiProvisioner.provision_credential(
            transport=mock_transport,
            local_filepath=local_key,
            remote_dest="/etc/ssl/key.pem"
        )

    # ZERO-LEAKAGE Validation: Check that rm -f was called on the corrupt payload!
    mock_transport.safe_run.assert_any_call("rm -f /etc/ssl/key.pem")

def test_pki_validator_destroys_mismatched_keys():
    mock_transport = MagicMock()
    
    def safe_run_side_effect(cmd, *args, **kwargs):
        if "x509" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout="MODULUS_CERT", stderr="", duration_s=0.1)
        elif "rsa" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout="MODULUS_KEY_MISMATCH", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_transport.safe_run.side_effect = safe_run_side_effect

    res = PkiPairingValidator.verify_x509_pairing(mock_transport, "/cert.pem", "/key.pem")
    
    assert not res.passed
    assert "mismatch" in res.error_msg
    # Verify ZERO-LEAKAGE logic triggered
    mock_transport.safe_run.assert_any_call("rm -f /cert.pem /key.pem")
