# src/pytest_mes_core/config.py
import tomllib
from enum import Enum
from pathlib import Path
from typing import Dict, Literal, Annotated, Union, TypeVar, Type, List, Optional
from pydantic import BaseModel, Field

class TimeDaemonType(str, Enum):
    CHRONY = "chrony"
    NTPD = "ntpd"
    SYSTEMD = "systemd-timesyncd"
    PTP = "linuxptp" # Precision Time Protocol (IEEE 1588)
    NONE = "none"


class BaseHardwareConfig(BaseModel):
    """
    Inherited by all physical device configs.
    Allows factory managers to dynamically toggle or soften hardware requirements.
    """
    enabled: bool = Field(
        default=True,
        description="If false, the framework completely ignores this device definition."
    )
    required: bool = Field(
        default=True,
        description="If true, bind failures (e.g., unplugged USB) are FATAL. If false, they just log a warning."
    )

# --- Station Metadata & Telemetry ---
class StationMetaConfig(BaseHardwareConfig):
    facility: str = Field(description="Factory location (e.g., 'Shenzhen-Line-1')")
    jig_id: str = Field(description="Unique ID of this specific test fixture")

class TelemetryConfig(BaseHardwareConfig):
    """Controls where the immutable test records are shipped."""
    exporter_type: Literal["jsonl", "influxdb", "rest"] = "jsonl"
    log_directory: str = Field(default="/var/log/mes_core")
    influx_url: Optional[str] = None

# --- COTS PSU Configs ---
class RigolPsuConfig(BaseHardwareConfig):
    vendor: Literal["rigol"]
    ip_address: str
    channel: int = Field(default=1, ge=1, le=3)

class KeysightPsuConfig(BaseHardwareConfig):
    vendor: Literal["keysight"]
    visa_resource: str

PsuVendorConfig = Annotated[
    Union[RigolPsuConfig, KeysightPsuConfig],
    Field(discriminator="vendor")
]

# --- Protocol Configs ---
class I2cEepromConfig(BaseHardwareConfig):
    bus: int
    address: str # e.g., "0x50"
    test_register: str = "0x00"
    num_bytes: int = Field(default=16, gt=0)
    page_size_bytes: int = Field(default=16, gt=0)
    write_delay_s: float = Field(default=0.02, gt=0)

class MtdFlashConfig(BaseHardwareConfig):
    mtd_dev: str = "/dev/mtd1"
    sector_offset_hex: str = "0x000000"
    erase_blocks: int = Field(default=1, gt=0)
    test_bytes: int = Field(default=256, gt=0)
    page_size_bytes: int = Field(default=256, gt=0)

class BlockStorageConfig(BaseHardwareConfig):
    mount_point: str = "/mnt/sdcard"
    test_file_size_mb: int = Field(default=50, gt=0)
    min_write_mbps: float = Field(default=10.0, gt=0)
    block_size_str: str = Field(default="4M", pattern=r"^[0-9]+[KMG]$")

class EthernetConfig(BaseHardwareConfig):
    interface: str = "eth0"
    expected_speed_mbps: int = 1000
    iperf_min_mbps: float = 850.0
    iperf_duration_s: int = 5
    mtu: int = Field(default=1500, description="Forces MTU. 9000 for Jumbo Frames.")
    dut_static_ip: str = Field(default="192.168.100.2/24", description="CIDR forced onto DUT.")
    host_iperf_ip: str = Field(default="192.168.100.1", description="Host PC's static IP on this subnet.")
    iperf_port: int = Field(default=5201, description="Specify port to avoid collisions.")

class CanConfig(BaseHardwareConfig):
    dut_interface: str = "can0"
    test_id: int = Field(default=0x123)
    payload: List[int] = Field(default=[0xDE, 0xAD, 0xBE, 0xEF])
    timeout_s: float = Field(default=2.0, gt=0)

class UartConfig(BaseHardwareConfig):
    dut_device: str = "/dev/ttymxc2"
    baudrate: int = Field(default=115200, gt=0)
    test_string: str = "MES_UART_SYNC"
    timeout_s: float = Field(default=2.0, gt=0)

class GpioEdgeConfig(BaseHardwareConfig):
    gpiochip: int
    line: int
    timeout_s: int = Field(default=5, gt=0)
    edge_type: Literal["rising-edge", "falling-edge", "both-edges"] = "rising-edge"

class GpioLedConfig(BaseHardwareConfig):
    gpiochip: int
    line: int

class GpioLoopbackConfig(BaseHardwareConfig):
    tx_gpiochip: int
    tx_line: int
    rx_gpiochip: int
    rx_line: int
    settling_time_s: float = Field(default=0.05, gt=0.0, description="Optocoupler/Relay propagation delay")

class EfuseConfig(BaseHardwareConfig):
    nvmem_path: str
    dmesg_grep_pattern: str = Field(default="ocotp|nvmem|fuse")
    require_32bit_alignment: bool = Field(default=True)

class IioAdcConfig(BaseHardwareConfig):
    # UPDATED: Replaced integer with string to sync with dynamic probe-order logic
    sensor_name: str = Field(description="Dynamic sysfs string (e.g., 'ads1015') to defeat kernel probe races.")
    channel: int = Field(description="The numeric channel index (e.g. 0 for in_voltage0)")
    samples: int = Field(default=10, gt=0)
    delay_s: float = Field(default=0.01, gt=0)
    min_v: float = Field(default=0.0, description="Lower acceptable physical boundary")
    max_v: float = Field(default=5.0, description="Upper acceptable physical boundary")

class IioDacConfig(BaseHardwareConfig):
    sensor_name: str
    channel: int

# --- Provisioning ---
class JtagTargetConfig(BaseHardwareConfig):
    interface_cfg: str = Field(description="OpenOCD interface (e.g., 'interface/jlink.cfg')")
    target_cfg: str = Field(description="OpenOCD target (e.g., 'target/stm32f4x.cfg')")
    rpc_port: int = Field(
        default=4444,
        description="MUST be unique for every JTAG probe to prevent socket collisions."
    )
    firmware_path: str = Field(description="Absolute path to the .bin or .hex payload")
    timeout_s: int = Field(default=120)

class BootstrapConfig(BaseHardwareConfig):
    """Configuration for physical Host PC GPIOs that control the DUT's Boot/Reset state."""
    gpiochip: int = Field(default=0, description="Host PC gpiochip index")

    # We now support 1 to 4 pins to handle complex SoC boot multiplexers
    boot_pins: List[int] = Field(
        default_factory=list,
        max_length=4,
        description="List of Host GPIO pins connected to DUT BOOT_MODE[0..3]"
    )
    reset_pin: int = Field(description="Host GPIO pin connected to DUT RESET")
    reset_active_low: bool = Field(default=True, description="True if 0V holds the DUT in reset")

    # Dynamic dictionary mapping a logical state to a specific pin configuration
    boot_modes: Dict[str, List[int]] = Field(
        default_factory=lambda: {
            "recovery": [1], # Fallback defaults for simple 1-pin chips
            "normal": [0]
        },
        description="Maps named boot modes to a list of logic levels matching the boot_pins array order."
    )

class UsbSdMuxConfig(BaseHardwareConfig):
    serial_id: str = Field(description="USB serial number of the mux, e.g., 'mux-01'")
    host_block_device: str = Field(description="Expected Host mount point, e.g., '/dev/sda' or '/dev/mmcblk0'")
    image_flash_timeout_s: int = Field(default=300, gt=0)

class TeziProvisioningConfig(BaseHardwareConfig):
    tezi_folder_path: str = Field(description="Path to the extracted TEZI image folder containing uuu.auto")
    usb_recovery_timeout_s: int = Field(default=30, gt=0, description="Time to wait for operator to put board in Recovery Mode")
    flash_timeout_s: int = Field(default=300, gt=0)

class MicrochipIcpConfig(BaseHardwareConfig):
    """Configuration for In-Circuit Programming via Microchip IPECMD."""
    ipecmd_path: str = Field(
        default="/opt/microchip/mplabx/v6.15/mplab_platform/mplab_ipe/ipecmd.jar",
        description="Absolute path to the headless Java flashing tool."
    )
    device: str = Field(description="The silicon part number, e.g., 'PIC18F45K22'")
    tool_serial: str = Field(description="The exact USB serial number of the physical PICkit 4/5")
    firmware_path: str = Field(description="Absolute path to the .hex payload")
    flash_timeout_s: int = Field(default=45, gt=0)
    mutex_timeout_s: float = Field(default=60.0, description="How long to wait for the USB hardware lock")

class SshTargetConfig(BaseHardwareConfig):
    ip_address: str
    user: str = Field(default="root")
    password: Optional[str] = None
    identity_file: Optional[str] = Field(default=None, description="Absolute path to SSH private key/certificate")
    port: int = Field(default=22, gt=0, le=65535)
    connect_timeout_s: float = Field(default=5.0, gt=0)

class BootProfilerConfig(BaseHardwareConfig):
    port: str = Field(description="Host PC physical UART port, e.g., /dev/ttyUSB0")
    baudrate: int = Field(default=115200, gt=0)
    milestones: Union[Dict[str, str], List[str]] = Field(default_factory=dict)
    timeout_s: float = Field(default=60.0, gt=0)

class HostCanConfig(BaseHardwareConfig):
    interface: str = Field(default="can0", description="Host socketcan interface")
    bitrate: int = Field(default=500000, gt=0)

class HostSerialConfig(BaseHardwareConfig):
    port: str = Field(description="Host PC physical UART port (e.g., /dev/ttyUSB0)")
    baudrate: int = Field(default=115200, gt=0)
    timeout_s: float = Field(default=1.0, gt=0)

class HidScannerConfig(BaseHardwareConfig):
    device_name_substring: str = Field(default="Barcode Scanner", description="String to match in evdev list")
    scan_timeout_s: int = Field(default=30, gt=0, description="Max time to wait for operator to scan")

class EStopConfig(BaseHardwareConfig):
    gpiochip: int = Field(default=0, description="Linux gpiochip index on the Host PC")
    line: int = Field(description="Physical GPIO pin number")
    active_low: bool = Field(default=True, description="True if pressing E-Stop drops voltage to 0V")
    polling_interval_s: float = Field(default=0.05, gt=0.0)

class SysfsTargetConfig(BaseHardwareConfig):
    path: str
    scale: float = Field(default=1.0)
    unit: str = Field(default="", description="Optional unit string for Grafana (e.g., 'C', 'MHz', 'V')")

class SysfsPollerConfig(BaseHardwareConfig):
    polling_interval_s: float = Field(default=1.0, gt=0.1)
    targets: Dict[str, SysfsTargetConfig] = Field(description="Maps telemetry aliases to scaled sysfs targets.")

class ExecutableConfig(BaseHardwareConfig):
    binary_path: str = Field(description="Absolute path to the executable on the DUT")
    arguments: str = Field(default="")
    expected_exit_code: int = Field(default=0)
    timeout_s: float = Field(default=30.0, gt=0.0)
    log_file_path: Optional[str] = Field(default=None)

class MmioConfig(BaseHardwareConfig):
    address_hex: str = Field(description="Physical memory address (e.g., '0x30330000')")
    data_width: Literal[8, 16, 32, 64] = Field(default=32, description="Access width in bits")
    expected_value_hex: Optional[str] = Field(default=None)
    bit_mask_hex: str = Field(default="0xFFFFFFFF", description="Mask to isolate specific bits")

class TimeSyncConfig(BaseHardwareConfig):
    max_drift_s: float = Field(default=5.0)
    force_host_sync: bool = Field(default=True)
    daemon_type: TimeDaemonType = Field(default=TimeDaemonType.CHRONY)
    rtc_paths: List[str] = Field(default_factory=lambda: ["/sys/class/rtc/rtc0", "/sys/class/rtc/rtc1"])
    chrony_cmd: str = Field(default="chronyc tracking")
    pps_devices: List[str] = Field(default_factory=lambda: ["/dev/pps0"])
    verify_hardware_pps: bool = Field(default=False)
    verify_chrony_pps: bool = Field(default=False)

# --- ROOT STATION ENVIRONMENT ---
class StationEnvironment(BaseHardwareConfig):
    """
    The Indisputable Hardware BOM.
    Injects physical reality into the test session.
    """
    station_meta: StationMetaConfig = Field(
        description="Factory location and Jig ID metadata."
    )
    telemetry: TelemetryConfig = Field(
        default_factory=TelemetryConfig,
        description="Routing rules for Grafana/JSONL telemetry."
    )

    psu_hardware: Optional[PsuVendorConfig] = Field(
        default=None,
        description="Configures COTS power supplies (Rigol, Keysight) over LAN/VISA."
    )
    e_stop: Optional[EStopConfig] = Field(
        default=None,
        description="Physical Emergency Stop hardware binding on the Host PC."
    )

    # Multi-Instance Hardware Dictionaries
    ethernet: Dict[str, EthernetConfig] = Field(
        default_factory=dict,
        description="DUT Ethernet interfaces for iPerf3 speed validation."
    )
    can_bus: Dict[str, CanConfig] = Field(
        default_factory=dict,
        description="DUT CAN interfaces and predefined test payloads."
    )
    uart: Dict[str, UartConfig] = Field(
        default_factory=dict,
        description="DUT UART devices for loopback or synchronization testing."
    )
    i2c_eeprom: Dict[str, I2cEepromConfig] = Field(
        default_factory=dict,
        description="I2C EEPROM devices for strict read/write validation."
    )
    mtd_flash: Dict[str, MtdFlashConfig] = Field(
        default_factory=dict,
        description="Raw MTD flash partitions for erase/write block testing."
    )
    block_storage: Dict[str, BlockStorageConfig] = Field(
        default_factory=dict,
        description="Mounted block devices (SD/eMMC) for throughput testing."
    )
    efuse: Optional[EfuseConfig] = Field(
        default=None,
        description="Hardware-specific parameters for SoC OTP/eFuse controllers."
    )
    adc: Dict[str, IioAdcConfig] = Field(
        default_factory=dict,
        description="IIO ADC sensors (e.g., voltage, current) with dynamic probe-order support."
    )
    dac: Dict[str, IioDacConfig] = Field(
        default_factory=dict,
        description="IIO DAC endpoints for signal generation."
    )
    gpio_edge: Dict[str, GpioEdgeConfig] = Field(
        default_factory=dict,
        description="DUT GPIOs configured to catch rising/falling edge hardware interrupts."
    )
    gpio_led: Dict[str, GpioLedConfig] = Field(
        default_factory=dict,
        description="DUT GPIOs connected to LEDs for visual or automated optical inspection."
    )
    gpio_loopback: Dict[str, GpioLoopbackConfig] = Field(
        default_factory=dict,
        description="Pairs of TX/RX GPIOs for physical loopback testing via optocouplers/relays."
    )
    usb_sd_mux: Dict[str, UsbSdMuxConfig] = Field(
        default_factory=dict,
        description="Linux Automation USB-SD-Mux devices for physical SD card flashing."
    )
    tezi_provisioning: Dict[str, TeziProvisioningConfig] = Field(
        default_factory=dict,
        description="Toradex Easy Installer payload configurations."
    )
    ssh_targets: Dict[str, SshTargetConfig] = Field(
        default_factory=dict,
        description="SSH credentials and targets for the main FailoverTransport."
    )
    boot_profilers: Dict[str, BootProfilerConfig] = Field(
        default_factory=dict,
        description="UART-based boot time profilers mapping dmesg regex patterns to timestamps."
    )
    host_can: Dict[str, HostCanConfig] = Field(
        default_factory=dict,
        description="Host PC physical CAN adapters (e.g., PCAN-USB, Kvaser, socketcan)."
    )
    host_serial: Dict[str, HostSerialConfig] = Field(
        default_factory=dict,
        description="Host PC physical UART adapters (e.g., FTDI cables) with exclusive OS locks."
    )
    hid_scanners: Dict[str, HidScannerConfig] = Field(
        default_factory=dict,
        description="Physical USB barcode scanners attached to the Host PC for serialization."
    )
    custom_executables: Dict[str, ExecutableConfig] = Field(
        default_factory=dict,
        description="Custom compiled binaries (e.g., RF calibration tools) to execute directly on the DUT."
    )
    sysfs_profilers: Dict[str, SysfsPollerConfig] = Field(
        default_factory=dict,
        description="Background telemetry pollers for DUT sysfs endpoints (e.g., CPU temp, fan speed)."
    )
    mmio_registers: Dict[str, MmioConfig] = Field(
        default_factory=dict,
        description="Direct memory-mapped IO registers to read/verify across the SoC bus."
    )

    # --- Advanced Provisioning & Context ---
    post_mortem_dumps: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Maps test names (or substrings) to a list of bash commands to execute on the DUT if the test fails, capturing context before teardown."
    )
    jtag_targets: Dict[str, JtagTargetConfig] = Field(
        default_factory=dict,
        description="Maps logical components to specific OpenOCD JTAG/SWD configurations and unique RPC ports."
    )
    microchip_targets: Dict[str, MicrochipIcpConfig] = Field(
        default_factory=dict,
        description="Maps logical components to Microchip IPECMD configurations locked to specific PICkit serials."
    )

    # --- THE RESTORED MISSING LINKS ---
    bootstrap: Optional[BootstrapConfig] = Field(
        default=None,
        description="Physical Boot/Reset pin multiplexing config. Defines how the Host PC forces the DUT into recovery or eMMC execution."
    )
    time_sync: Optional[TimeSyncConfig] = Field(
        default=None,
        description="NTP/PTP and hardware RTC validation bounds. Enforces temporal accuracy across the test jig and DUT."
    )

# --- Generic TOML Loader ---
T = TypeVar('T', bound=BaseModel)

def load_toml_config(filepath: Path, config_model: Type[T]) -> T:
    if not filepath.exists():
        raise FileNotFoundError(f"FATAL: MES environment configuration missing at {filepath}")
    with open(filepath, "rb") as f:
        data = tomllib.load(f)
    return config_model(**data)
