# src/pytest_mes_core/config.py
import tomllib
from pathlib import Path
from typing import Dict, Literal, Annotated, Union, TypeVar, Type, List, Optional
from pydantic import BaseModel, Field

# --- Station Metadata ---
class StationMetaConfig(BaseModel):
    facility: str
    jig_id: str

# --- COTS PSU Configs ---
class RigolPsuConfig(BaseModel):
    vendor: Literal["rigol"]
    ip_address: str
    channel: int = Field(default=1, ge=1, le=3)

class KeysightPsuConfig(BaseModel):
    vendor: Literal["keysight"]
    visa_resource: str

PsuVendorConfig = Annotated[
    Union[RigolPsuConfig, KeysightPsuConfig],
    Field(discriminator="vendor")
]

# --- Protocol Configs ---
class I2cEepromConfig(BaseModel):
    bus: int
    address: str # e.g., "0x50"
    test_register: str = "0x00"
    num_bytes: int = Field(default=16, gt=0)
    page_size_bytes: int = Field(default=16, gt=0)
    write_delay_s: float = Field(default=0.02, gt=0)

class MtdFlashConfig(BaseModel):
    mtd_dev: str = "/dev/mtd1"
    sector_offset_hex: str = "0x000000"
    erase_blocks: int = Field(default=1, gt=0)
    test_bytes: int = Field(default=256, gt=0)
    page_size_bytes: int = Field(default=256, gt=0)

class BlockStorageConfig(BaseModel):
    mount_point: str = "/mnt/sdcard"
    test_file_size_mb: int = Field(default=50, gt=0)
    min_write_mbps: float = Field(default=10.0, gt=0)
    block_size_str: str = Field(default="4M", pattern=r"^[0-9]+[KMG]$")

class EthernetConfig(BaseModel):
    interface: str = "eth0"
    expected_speed_mbps: int = Field(default=100, gt=0)
    host_iperf_ip: str
    iperf_duration_s: int = Field(default=3, gt=0)
    iperf_min_mbps: float = Field(default=85.0, gt=0.0)

class CanConfig(BaseModel):
    dut_interface: str = "can0"
    test_id: int = Field(default=0x123)
    payload: List[int] = Field(default=[0xDE, 0xAD, 0xBE, 0xEF])
    timeout_s: float = Field(default=2.0, gt=0)

class UartConfig(BaseModel):
    dut_device: str = "/dev/ttymxc2"
    baudrate: int = Field(default=115200, gt=0)
    test_string: str = "MES_UART_SYNC"
    timeout_s: float = Field(default=2.0, gt=0)

class GpioEdgeConfig(BaseModel):
    gpiochip: int
    line: int
    timeout_s: int = Field(default=5, gt=0)
    edge_type: Literal["rising-edge", "falling-edge", "both-edges"] = "rising-edge"

class GpioLedConfig(BaseModel):
    gpiochip: int
    line: int

# RESTORED: GpioLoopbackConfig
class GpioLoopbackConfig(BaseModel):
    tx_gpiochip: int
    tx_line: int
    rx_gpiochip: int
    rx_line: int
    settling_time_s: float = Field(default=0.05, gt=0.0, description="Optocoupler/Relay propagation delay")

class EfuseConfig(BaseModel):
    nvmem_path: str = "/sys/bus/nvmem/devices/imx-ocotp0/nvmem"
    offset_hex: str
    expected_burn_time_s: float = Field(default=0.5, gt=0)

class IioAdcConfig(BaseModel):
    iio_device: int
    channel: int
    samples: int = Field(default=10, gt=0)
    delay_s: float = Field(default=0.01, gt=0)

class IioDacConfig(BaseModel):
    iio_device: int
    channel: int

class UsbSdMuxConfig(BaseModel):
    serial_id: str = Field(description="USB serial number of the mux, e.g., 'mux-01'")
    host_block_device: str = Field(description="Expected Host mount point, e.g., '/dev/sda' or '/dev/mmcblk0'")
    image_flash_timeout_s: int = Field(default=300, gt=0)

class TeziProvisioningConfig(BaseModel):
    tezi_folder_path: str = Field(description="Path to the extracted TEZI image folder containing uuu.auto")
    usb_recovery_timeout_s: int = Field(default=30, gt=0, description="Time to wait for operator to put board in Recovery Mode")
    flash_timeout_s: int = Field(default=300, gt=0)

class SshTargetConfig(BaseModel):
    ip_address: str
    user: str = Field(default="root")
    password: Optional[str] = None
    identity_file: Optional[str] = Field(default=None, description="Absolute path to SSH private key/certificate")
    port: int = Field(default=22, gt=0, le=65535)
    connect_timeout_s: float = Field(default=5.0, gt=0)

class BootProfilerConfig(BaseModel):
    port: str = Field(description="Host PC physical UART port, e.g., /dev/ttyUSB0")
    baudrate: int = Field(default=115200, gt=0)
    milestones: Union[Dict[str, str], List[str]] = Field(
        default_factory=dict,
        description="Either a Dict of 'metric_name': 'regex', or a List of regex strings."
    )
    timeout_s: float = Field(default=60.0, gt=0)

class HostCanConfig(BaseModel):
    interface: str = Field(default="can0", description="Host socketcan interface")
    bitrate: int = Field(default=500000, gt=0)

class HostSerialConfig(BaseModel):
    port: str = Field(description="Host PC physical UART port (e.g., /dev/ttyUSB0)")
    baudrate: int = Field(default=115200, gt=0)
    timeout_s: float = Field(default=1.0, gt=0)

class HidScannerConfig(BaseModel):
    device_name_substring: str = Field(default="Barcode Scanner", description="String to match in evdev list")
    scan_timeout_s: int = Field(default=30, gt=0, description="Max time to wait for operator to scan")

# RESTORED: EStopConfig
class EStopConfig(BaseModel):
    gpiochip: int = Field(default=0, description="Linux gpiochip index on the Host PC")
    line: int = Field(description="Physical GPIO pin number")
    active_low: bool = Field(default=True, description="True if pressing E-Stop drops voltage to 0V")
    polling_interval_s: float = Field(default=0.05, gt=0.0)

class SysfsTargetConfig(BaseModel):
    path: str
    scale: float = Field(
        default=1.0,
        description="Multiplier to convert raw kernel integer to human units (e.g., 0.001 for milli-units, 0.001 for kHz to MHz)"
    )
    unit: str = Field(default="", description="Optional unit string for Grafana (e.g., 'C', 'MHz', 'V')")

class SysfsPollerConfig(BaseModel):
    polling_interval_s: float = Field(default=1.0, gt=0.1)
    targets: Dict[str, SysfsTargetConfig] = Field(
        description="Maps telemetry aliases to scaled sysfs targets."
    )

class ExecutableConfig(BaseModel):
    binary_path: str = Field(description="Absolute path to the executable on the DUT (e.g., /usr/bin/rf_cal)")
    arguments: str = Field(default="", description="CLI arguments to pass to the binary")
    expected_exit_code: int = Field(default=0)
    timeout_s: float = Field(default=30.0, gt=0.0)
    log_file_path: Optional[str] = Field(
        default=None,
        description="Optional absolute path to a log file the binary generates on the DUT."
    )

class MmioConfig(BaseModel):
    address_hex: str = Field(description="Physical memory address (e.g., '0x30330000')")
    data_width: Literal[8, 16, 32, 64] = Field(default=32, description="Access width in bits")
    expected_value_hex: Optional[str] = Field(default=None)
    bit_mask_hex: str = Field(default="0xFFFFFFFF", description="Mask to isolate specific bits")


# --- ROOT STATION ENVIRONMENT ---
class StationEnvironment(BaseModel):
    """
    The Indisputable Hardware BOM.
    Injects physical reality into the test session.
    """
    station_meta: StationMetaConfig
    psu_hardware: Optional[PsuVendorConfig] = None

    # RESTORED: e_stop
    e_stop: Optional[EStopConfig] = None

    # Multi-Instance Hardware Dictionaries
    ethernet: Dict[str, EthernetConfig] = Field(default_factory=dict)
    can_bus: Dict[str, CanConfig] = Field(default_factory=dict)
    uart: Dict[str, UartConfig] = Field(default_factory=dict)
    i2c_eeprom: Dict[str, I2cEepromConfig] = Field(default_factory=dict)
    mtd_flash: Dict[str, MtdFlashConfig] = Field(default_factory=dict)
    block_storage: Dict[str, BlockStorageConfig] = Field(default_factory=dict)
    efuse: Dict[str, EfuseConfig] = Field(default_factory=dict)
    adc: Dict[str, IioAdcConfig] = Field(default_factory=dict)
    dac: Dict[str, IioDacConfig] = Field(default_factory=dict)
    gpio_edge: Dict[str, GpioEdgeConfig] = Field(default_factory=dict)
    gpio_led: Dict[str, GpioLedConfig] = Field(default_factory=dict)

    # RESTORED: gpio_loopback
    gpio_loopback: Dict[str, GpioLoopbackConfig] = Field(default_factory=dict)

    usb_sd_mux: Dict[str, UsbSdMuxConfig] = Field(default_factory=dict)
    tezi_provisioning: Dict[str, TeziProvisioningConfig] = Field(default_factory=dict)
    ssh_targets: Dict[str, SshTargetConfig] = Field(default_factory=dict)
    boot_profilers: Dict[str, BootProfilerConfig] = Field(default_factory=dict)
    host_can: Dict[str, HostCanConfig] = Field(default_factory=dict)
    host_serial: Dict[str, HostSerialConfig] = Field(default_factory=dict)
    hid_scanners: Dict[str, HidScannerConfig] = Field(default_factory=dict)
    custom_executables: Dict[str, ExecutableConfig] = Field(default_factory=dict)
    sysfs_profilers: Dict[str, SysfsPollerConfig] = Field(default_factory=dict)

    # ADDED: mmio_registers
    mmio_registers: Dict[str, MmioConfig] = Field(default_factory=dict)

    post_mortem_dumps: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Maps test names (or substrings) to a list of bash commands to execute on failure."
    )

# --- Generic TOML Loader ---
T = TypeVar('T', bound=BaseModel)

def load_toml_config(filepath: Path, config_model: Type[T]) -> T:
    if not filepath.exists():
        raise FileNotFoundError(f"FATAL: MES environment configuration missing at {filepath}")
    with open(filepath, "rb") as f:
        data = tomllib.load(f)
    return config_model(**data)
