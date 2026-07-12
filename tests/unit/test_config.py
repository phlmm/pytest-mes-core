# tests/unit/test_config.py
import pytest
from pydantic import ValidationError, TypeAdapter
from pytest_mes_core.config import (
    PsuVendorConfig, RigolPsuConfig, KeysightPsuConfig, StateMachineConfig,
    HostSerialConfig, HostMqttConfig,
)

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
    with pytest.raises(ValidationError, match="does not match any of the expected tags"):
        adapter.validate_python(bad_data)


def test_state_machine_config_fix6_fields_roundtrip():
    """Fix 6: gpio_reset_pin, gpio_recovery_pin, recovery_latch_time_s,
    power_off_threshold_a, boot_straps_gpio_map, and storage_data_encrypted
    must be real declared fields (previously the FSM read them via
    getattr(self.cfg, ..., default) and pydantic's default extra='ignore'
    silently dropped them from TOML input)."""
    data = {
        "gpio_reset_pin": "RESET_N",
        "gpio_recovery_pin": "BOOT0",
        "recovery_latch_time_s": 2.5,
        "power_off_threshold_a": 0.1,
        "boot_straps_gpio_map": {"emmc": {"BOOT0": True, "BOOT1": False}},
        "storage_data_encrypted": "/dev/mapper/custom_crypt",
    }
    cfg = StateMachineConfig.model_validate(data)

    assert cfg.gpio_reset_pin == "RESET_N"
    assert cfg.gpio_recovery_pin == "BOOT0"
    assert cfg.recovery_latch_time_s == 2.5
    assert cfg.power_off_threshold_a == 0.1
    assert cfg.boot_straps_gpio_map == {"emmc": {"BOOT0": True, "BOOT1": False}}
    assert cfg.storage_data_encrypted == "/dev/mapper/custom_crypt"


def test_state_machine_config_fix6_fields_defaults_preserve_prior_behavior():
    """Defaults must match the previous getattr(..., default) fallback values
    exactly, so boards without these keys in TOML keep behaving as before."""
    cfg = StateMachineConfig()

    assert cfg.gpio_reset_pin is None
    assert cfg.gpio_recovery_pin == "RECOVERY_BTN"
    assert cfg.recovery_latch_time_s == 1.5
    assert cfg.power_off_threshold_a == 0.05
    assert cfg.boot_straps_gpio_map == {}
    assert cfg.storage_data_encrypted == "/dev/mapper/data_crypt"


def test_host_serial_config_os_shell_prompt_roundtrip():
    """Fix 2: HostSerialConfig must declare os_shell_prompt as a real field so
    TOML-provided prompts are no longer silently dropped by pydantic's default
    extra='ignore' behavior."""
    cfg = HostSerialConfig(port="/dev/ttyUSB0", os_shell_prompt="root@dut:")
    assert cfg.os_shell_prompt == "root@dut:"


def test_host_serial_config_os_shell_prompt_default():
    cfg = HostSerialConfig(port="/dev/ttyUSB0")
    assert cfg.os_shell_prompt == "~#"


def test_host_mqtt_config_tls_insecure_roundtrip():
    """Fix 7: tls_insecure defaults True (preserves existing factory behavior)
    and can be explicitly disabled to enable certificate verification."""
    cfg_default = HostMqttConfig(broker_ip="10.0.0.1")
    assert cfg_default.tls_insecure is True

    cfg_verify = HostMqttConfig(broker_ip="10.0.0.1", tls=True, tls_insecure=False, ca_cert_path="/etc/ca.pem")
    assert cfg_verify.tls_insecure is False
