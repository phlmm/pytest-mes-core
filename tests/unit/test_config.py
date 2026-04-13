# tests/unit/test_config.py
import pytest
from pydantic import ValidationError, TypeAdapter
from pytest_mes_core.config import PsuVendorConfig, RigolPsuConfig, KeysightPsuConfig

def test_psu_discriminated_union_routing():
    """Proves Pydantic dynamically routes vendor configs based strictly on the 'vendor' string."""

    adapter = TypeAdapter(PsuVendorConfig)

    # 1. Test Rigol Routing
    rigol_data = {"vendor": "rigol", "ip_address": "192.168.1.10", "channel": 2}
    parsed_rigol = adapter.validate_python(rigol_data)
    assert isinstance(parsed_rigol, RigolPsuConfig)
    assert parsed_rigol.ip_address == "192.168.1.10"

    # 2. Test Keysight Routing
    keysight_data = {"vendor": "keysight", "visa_resource": "USB0::1234::INSTR"}
    parsed_keysight = adapter.validate_python(keysight_data)
    assert isinstance(parsed_keysight, KeysightPsuConfig)

    # 3. Test Typo Rejection (The Factory Firewall)
    bad_data = {"vendor": "fluke", "ip_address": "10.0.0.1"}
    with pytest.raises(ValidationError, match="Input should be"):
        adapter.validate_python(bad_data)
