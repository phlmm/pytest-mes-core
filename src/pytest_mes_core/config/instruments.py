from typing import Dict, List, Literal, Optional, Union
from pydantic import Field, model_validator
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

PsuVendorConfig = Annotated[
    Union[RigolPsuConfig, KeysightPsuConfig],
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
    host_block_device: str
    image_flash_timeout_s: int = Field(default=300, gt=0)

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
