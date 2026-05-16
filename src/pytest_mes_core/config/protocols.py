from typing import Dict, List, Literal, Optional
from pydantic import Field, SecretStr
from pytest_mes_core.config.base import BaseHardwareConfig, TimeDaemonType

class I2cEepromConfig(BaseHardwareConfig):
    bus: int
    address: str
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
    mtu: int = Field(default=1500)
    dut_static_ip: str = Field(default="192.168.100.2/24")
    host_iperf_ip: str = Field(default="192.168.100.1")
    iperf_port: int = Field(default=5201)

class CanConfig(BaseHardwareConfig):
    dut_interface: str = "can0"
    bitrate: int = Field(default=500000, gt=0)
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
    settling_time_s: float = Field(default=0.05, gt=0.0)

class EfuseConfig(BaseHardwareConfig):
    nvmem_path: str
    dmesg_grep_pattern: str = Field(default="ocotp|nvmem|fuse")
    require_32bit_alignment: bool = Field(default=True)

class IioAdcConfig(BaseHardwareConfig):
    sensor_name: str = Field(description="Dynamic sysfs string (e.g., 'ads1015')")
    channel: int
    samples: int = Field(default=10, gt=0)
    delay_s: float = Field(default=0.01, gt=0)
    min_v: float = Field(default=0.0)
    max_v: float = Field(default=5.0)

class IioDacConfig(BaseHardwareConfig):
    sensor_name: str
    channel: int

class SshTargetConfig(BaseHardwareConfig):
    ip_address: str
    user: str = Field(default="root")
    password: Optional[SecretStr] = None
    identity_file: Optional[str] = Field(default=None)
    port: int = Field(default=22, gt=0, le=65535)
    connect_timeout_s: float = Field(default=5.0, gt=0)
    forensic_journaling: bool = Field(default=False)

    def get_password(self) -> Optional[str]:
        return self.password.get_secret_value() if self.password else None

class HostCanConfig(BaseHardwareConfig):
    interface: str
    bustype: Literal["socketcan", "slcan"] = Field(default="socketcan")
    bitrate: int = Field(default=500000, gt=0)
    tty_baudrate: int = Field(default=2000000)

class HostSerialConfig(BaseHardwareConfig):
    port: str
    baudrate: int = Field(default=115200, gt=0)
    timeout_s: float = Field(default=1.0, gt=0)

class SysfsTargetConfig(BaseHardwareConfig):
    path: str
    scale: float = Field(default=1.0)
    unit: str = Field(default="")

class SysfsPollerConfig(BaseHardwareConfig):
    polling_interval_s: float = Field(default=1.0, gt=0.1)
    targets: Dict[str, SysfsTargetConfig] = Field(description="Maps aliases to sysfs targets.")

class ExecutableConfig(BaseHardwareConfig):
    binary_path: str
    arguments: str = Field(default="")
    expected_exit_code: int = Field(default=0)
    timeout_s: float = Field(default=30.0, gt=0.0)
    log_file_path: Optional[str] = Field(default=None)

class MmioConfig(BaseHardwareConfig):
    address_hex: str
    data_width: Literal[8, 16, 32, 64] = Field(default=32)
    expected_value_hex: Optional[str] = Field(default=None)
    bit_mask_hex: str = Field(default="0xFFFFFFFF")

class TimeSyncConfig(BaseHardwareConfig):
    max_drift_s: float = Field(default=5.0)
    force_host_sync: bool = Field(default=True)
    daemon_type: TimeDaemonType = Field(default=TimeDaemonType.CHRONY)
    rtc_paths: List[str] = Field(default_factory=lambda: ["/sys/class/rtc/rtc0", "/sys/class/rtc/rtc1"])
    chrony_cmd: str = Field(default="chronyc tracking")
    pps_devices: List[str] = Field(default_factory=lambda: ["/dev/pps0"])
    verify_hardware_pps: bool = Field(default=False)
    verify_chrony_pps: bool = Field(default=False)

class UsbStorageConfig(BaseHardwareConfig):
    vid_hex: str
    pid_hex: str
    expected_speed: str = Field(default="high-speed")
    test_size_mb: int = Field(default=5, gt=0)
    min_write_mbps: float = Field(default=5.0, gt=0.0)

class HostMqttConfig(BaseHardwareConfig):
    broker_ip: str
    port: int = Field(default=1883, gt=0, le=65535)
    tls: bool = Field(default=False)
    username: Optional[str] = None
    password: Optional[SecretStr] = None
    client_id: str = Field(default="pytest-mes-core")
    publish_topic: str = Field(default="mes/c2/cmd")
    subscribe_topic: str = Field(default="mes/c2/resp")
    timeout_s: float = Field(default=10.0, gt=0)

class UdpDiagnosticConfig(BaseHardwareConfig):
    bind_port: int = Field(default=8888, gt=0, le=65535)
    bind_address: str = Field(default="")
    multicast_group: Optional[str] = Field(default=None)
    buffer_size: int = Field(default=1024)
