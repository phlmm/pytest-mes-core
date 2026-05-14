from typing import Dict, List, Literal, Optional, Union
from pydantic import field_validator
from pydantic import Field, computed_field, model_validator
from typing_extensions import Annotated
from pytest_mes_core.config.base import BaseHardwareConfig

class RigolPsuConfig(BaseHardwareConfig):
    vendor: Literal["rigol"]
    ip_address: str
    channel: int = Field(default=1, ge=1, le=3)
    enable_data_logging: bool = Field(default=False)
    log_interval_s: float = Field(default=0.1, gt=0.0)

class KeysightPsuConfig(BaseHardwareConfig):
    vendor: Literal["keysight"]
    visa_resource: str
    enable_data_logging: bool = Field(default=False)
    log_interval_s: float = Field(default=0.1, gt=0.0)

class FnirsiPsuConfig(BaseHardwareConfig):
    vendor: Literal["fnirsi"]
    serial_port: str
    baudrate: int = Field(default=115200)
    enable_data_logging: bool = Field(default=False)
    log_interval_s: float = Field(default=0.1, gt=0.0)
    default_voltage: Optional[float] = None
    default_current: Optional[float] = None
    ovp_limit: Optional[float] = None
    ocp_limit: Optional[float] = None

PsuVendorConfig = Annotated[
    Union[RigolPsuConfig, KeysightPsuConfig, FnirsiPsuConfig],
    Field(discriminator="vendor")
]

class JtagTargetConfig(BaseHardwareConfig):
    interface_cfg: str
    target_cfg: str
    rpc_port: int = Field(default=4444)
    firmware_path: str
    timeout_s: int = Field(default=120)
    gdb_port: int = Field(default=3333)
    gdb_toolchain_path: str = Field(default="gdb-multiarch")
    dcc_buffer_address_hex: Optional[str] = Field(default=None)
    stack_pointer_address_hex: Optional[str] = Field(default=None)

    @model_validator(mode='after')
    def validate_port_collisions(self) -> 'JtagTargetConfig':
        if self.rpc_port == self.gdb_port:
            raise ValueError(f"RPC port and GDB port cannot both be {self.rpc_port}. They must be unique.")
        return self

class BootstrapConfig(BaseHardwareConfig):
    gpiochip: int = Field(default=0)
    boot_pins: List[int] = Field(default_factory=list, max_length=4)
    reset_pin: int
    reset_active_low: bool = Field(default=True)
    boot_modes: Dict[str, List[int]] = Field(
        default_factory=lambda: {"recovery": [1], "normal": [0]}
    )

class UsbSdMuxConfig(BaseHardwareConfig):
    serial_id: str
    # Accept either a stable by-id path (/dev/disk/by-id/...) or a raw node (/dev/sdX).
    # Prefer by-id so the path survives reboots. Use resolved_block_device at runtime.
    host_block_device: str
    image_flash_timeout_s: int = Field(default=300, gt=0)
    # Optional: when set, the HostUsbSdMuxAdapter will assert/release DUT recovery
    # by driving this GPIO on the USB-SD-Mux Fast variant (sdFST HS-SD/MMC).
    # Valid values: 0 or 1 (the two auxiliary open-drain outputs on the Fast variant).
    # Leave unset (None) for the Classic variant which has no user GPIOs.
    recovery_gpio: Optional[int] = Field(default=None)

    @field_validator('recovery_gpio')
    @classmethod
    def _validate_recovery_gpio(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in (0, 1):
            raise ValueError(f"recovery_gpio must be 0 or 1, got {v!r}")
        return v

    @computed_field
    @property
    def resolved_block_device(self) -> str:
        """
        Resolves the configured block device path to the real kernel device node.

        Follows symlinks at runtime, so a stable ``/dev/disk/by-id/...`` path in the
        TOML is transparently translated to the currently-assigned ``/dev/sdX`` node.
        Falls back to the raw value if the path doesn't exist (e.g. device unplugged).
        """
        from pathlib import Path
        p = Path(self.host_block_device)
        if p.exists():
            return str(p.resolve())
        return self.host_block_device

class TeziProvisioningConfig(BaseHardwareConfig):
    tezi_folder_path: str
    usb_recovery_timeout_s: int = Field(default=30, gt=0)
    flash_timeout_s: int = Field(default=300, gt=0)
    payload_uri: str
    payload_sha256: Optional[str] = Field(default=None)

    @model_validator(mode='after')
    def validate_crypto_requirements(self) -> 'TeziProvisioningConfig':
        if self.payload_sha256:
            if len(self.payload_sha256) != 64:
                raise ValueError("Provided payload_sha256 is not a valid 64-character SHA256 hash.")
            if not self.payload_uri.startswith("https://") and not self.payload_uri.startswith("http://"):
                raise ValueError("SHA256 validation requires a remote HTTP/HTTPS payload_uri.")
        return self

class MicrochipIcpConfig(BaseHardwareConfig):
    ipecmd_path: str = Field(default="/opt/microchip/mplabx/v6.15/mplab_platform/mplab_ipe/ipecmd.jar")
    device: str
    tool_serial: str
    firmware_path: str
    flash_timeout_s: int = Field(default=45, gt=0)
    mutex_timeout_s: float = Field(default=60.0)
    extra_flags: List[str] = Field(default_factory=lambda: ["W3.3"])

class BootProfilerConfig(BaseHardwareConfig):
    port: str
    baudrate: int = Field(default=115200, gt=0)
    timeout_s: float = Field(default=60.0, gt=0)
    auto_login_user: Optional[str] = None
    auto_login_password: Optional[str] = None
    milestones: Dict[str, str] = Field(default_factory=dict)

class HidScannerConfig(BaseHardwareConfig):
    device_name_substring: str = Field(default="Barcode Scanner")
    scan_timeout_s: int = Field(default=30, gt=0)

class EStopConfig(BaseHardwareConfig):
    gpiochip: int = Field(default=0)
    line: int
    active_low: bool = Field(default=True)
    polling_interval_s: float = Field(default=0.05, gt=0.0)
