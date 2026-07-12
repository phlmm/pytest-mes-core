from enum import Enum
from typing import Dict, Literal, Optional
from pydantic import BaseModel, Field, SecretStr

class TimeDaemonType(str, Enum):
    CHRONY = "chrony"
    NTPD = "ntpd"
    SYSTEMD = "systemd-timesyncd"
    PTP = "linuxptp"
    NONE = "none"

class BaseHardwareConfig(BaseModel):
    """Inherited by all physical device configs."""
    enabled: bool = Field(default=True, description="If false, the framework ignores this device.")
    required: bool = Field(default=True, description="If true, bind failures are FATAL.")

class StationMetaConfig(BaseHardwareConfig):
    facility: str = Field(description="Factory location (e.g., 'Shenzhen-Line-1')")
    jig_id: str = Field(description="Unique ID of this specific test fixture")
    environment: str = "lab"

class TelemetryConfig(BaseHardwareConfig):
    exporter_type: str = Field(default="jsonl")
    log_directory: str = Field(default="/var/log/mes_core")
    influx_url: Optional[str] = None

class GitAuthConfig(BaseModel):
    user: Optional[str] = Field(default=None, description="Git Username (for Basic Auth)")
    token: Optional[SecretStr] = Field(default=None, description="Git Bearer Token or Password")
    client_cert: Optional[str] = Field(default=None, description="Path to mutual TLS client certificate")
    client_key: Optional[str] = Field(default=None, description="Path to mutual TLS private key")
    ignore_ssl: bool = Field(default=False, description="Disable SSL certificate verification")

    def get_token(self) -> Optional[str]:
        return self.token.get_secret_value() if self.token else None

class StateMachineConfig(BaseHardwareConfig):
    bootloader_prompt: str = Field(default="=>")
    bootloader_interrupt_pattern: str = Field(default="stop autoboot")
    bootloader_interrupt_char: str = Field(default="\n")
    bootloader_boot_cmd: str = Field(default="boot")

    os_login_prompt: str = Field(default="login:")
    os_password_prompt: str = Field(default="Password:")
    os_shell_prompt: str = Field(default="root@")
    os_user: str = Field(default="root")

    #  Secure plaintext passwords
    os_password: Optional[SecretStr] = Field(default=None)

    os_ssh_public_key: Optional[str] = Field(default=None)
    immutable_rootfs: bool = Field(default=False)

    cold_boot_timeout_s: float = Field(default=60.0)
    autoboot_enabled: bool = Field(default=True)

    gpio_reset_pin: Optional[str] = Field(default=None, description="GPIO pin name for the DUT hardware RESET line.")
    gpio_recovery_pin: Optional[str] = Field(default="RECOVERY_BTN", description="GPIO pin name asserting the recovery strap.")
    recovery_latch_time_s: float = Field(default=1.5, description="Seconds to hold the recovery strap after power-on.")
    power_off_threshold_a: float = Field(default=0.05, description="PSU current below which the DUT is considered POWER_OFF.")
    boot_straps_gpio_map: Dict[str, Dict[str, bool]] = Field(default_factory=dict, description="Boot medium -> {gpio pin: level} strap map.")
    storage_data_encrypted: Optional[str] = Field(default="/dev/mapper/data_crypt", description="Encrypted data partition device to verify mounted; None/empty disables the check.")

    def get_os_password(self) -> Optional[str]:
        return self.os_password.get_secret_value() if self.os_password else None


class MqttBearerOverrideConfig(BaseModel):
    """
    Station-level MQTT bearer override.

    ``mode`` controls which network bearer the firmware will use when the
    test session starts.  Individual tests may override this dynamically
    via the ``mqtt_bearer_ctrl`` fixture.

    Values:
        "auto" — firmware selects the highest-priority ready bearer
                 (ETH > WiFi > LTE).
        "eth"  — force Ethernet bearer.
        "wifi" — force WiFi bearer (not yet implemented).
        "lte"  — force Quectel LTE modem bearer.
    """
    mode: Literal["auto", "eth", "wifi", "lte"] = Field(default="auto")
    sim_installed: bool = Field(
        default=False,
        description="True when a physical SIM is installed in the LTE modem.",
    )
    sim_apn: str = Field(
        default="internet",
        description="APN for the installed SIM (e.g. 'internet' for Yettel Bulgaria).",
    )
