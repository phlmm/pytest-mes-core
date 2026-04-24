from enum import Enum
from typing import Optional
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
    bootloader_prompt: str = Field(default="=> ")
    bootloader_interrupt_pattern: str = Field(default="stop autoboot")
    bootloader_interrupt_char: str = Field(default="\n")
    bootloader_boot_cmd: str = Field(default="boot")

    os_login_prompt: str = Field(default="login: ")
    os_password_prompt: str = Field(default="Password: ")
    os_shell_prompt: str = Field(default="root@")
    os_user: str = Field(default="root")

    #  Secure plaintext passwords
    os_password: Optional[SecretStr] = Field(default=None)

    os_ssh_public_key: Optional[str] = Field(default=None)
    immutable_rootfs: bool = Field(default=False)

    cold_boot_timeout_s: float = Field(default=60.0)
    autoboot_enabled: bool = Field(default=True)

    def get_os_password(self) -> Optional[str]:
        return self.os_password.get_secret_value() if self.os_password else None
