import structlog
import sys
import tomllib
import logging
from pathlib import Path
from typing import Dict, List, Optional, Type, TypeVar
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pytest_mes_core.config.base import StationMetaConfig, TelemetryConfig, StateMachineConfig, GitAuthConfig, MqttBearerOverrideConfig
from pytest_mes_core.config.instruments import PsuVendorConfig, EStopConfig, JtagTargetConfig, PyOcdTargetConfig, ProbeRsTargetConfig, MicrochipIcpConfig, BootstrapConfig, UsbSdMuxConfig, TeziProvisioningConfig, BootProfilerConfig, HidScannerConfig
from pytest_mes_core.config.protocols import EthernetConfig, CanConfig, UartConfig, I2cEepromConfig, MtdFlashConfig, BlockStorageConfig, EfuseConfig, IioAdcConfig, IioDacConfig, GpioEdgeConfig, GpioLedConfig, GpioLoopbackConfig, SshTargetConfig, HostCanConfig, HostSerialConfig, SysfsPollerConfig, ExecutableConfig, MmioConfig, TimeSyncConfig, UsbStorageConfig, HostMqttConfig, UdpDiagnosticConfig
logger = structlog.get_logger('mes_core.config')
T = TypeVar('T', bound=BaseModel)

class StationEnvironment(BaseModel):
    """The Indisputable Hardware BOM."""
    model_config = ConfigDict(extra='forbid')
    station_meta: StationMetaConfig
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    git_auth: Optional[GitAuthConfig] = None
    state_machine: Optional[StateMachineConfig] = None
    psu_hardware: Optional[PsuVendorConfig] = None
    e_stop: Optional[EStopConfig] = None
    ethernet: Dict[str, EthernetConfig] = Field(default_factory=dict)
    can_bus: Dict[str, CanConfig] = Field(default_factory=dict)
    uart: Dict[str, UartConfig] = Field(default_factory=dict)
    i2c_eeprom: Dict[str, I2cEepromConfig] = Field(default_factory=dict)
    mtd_flash: Dict[str, MtdFlashConfig] = Field(default_factory=dict)
    block_storage: Dict[str, BlockStorageConfig] = Field(default_factory=dict)
    efuse: Optional[EfuseConfig] = None
    adc: Dict[str, IioAdcConfig] = Field(default_factory=dict)
    dac: Dict[str, IioDacConfig] = Field(default_factory=dict)
    gpio_edge: Dict[str, GpioEdgeConfig] = Field(default_factory=dict)
    gpio_led: Dict[str, GpioLedConfig] = Field(default_factory=dict)
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
    mmio_registers: Dict[str, MmioConfig] = Field(default_factory=dict)
    usb_storage: Dict[str, UsbStorageConfig] = Field(default_factory=dict)
    host_mqtt: Dict[str, HostMqttConfig] = Field(default_factory=dict)
    udp_logs: Dict[str, UdpDiagnosticConfig] = Field(default_factory=dict)
    post_mortem_dumps: Dict[str, List[str]] = Field(default_factory=dict)
    jtag_targets: Dict[str, JtagTargetConfig] = Field(default_factory=dict)
    pyocd_targets: Dict[str, PyOcdTargetConfig] = Field(default_factory=dict)
    probe_rs_targets: Dict[str, ProbeRsTargetConfig] = Field(default_factory=dict)
    microchip_targets: Dict[str, MicrochipIcpConfig] = Field(default_factory=dict)
    bootstrap: Optional[BootstrapConfig] = None
    time_sync: Optional[TimeSyncConfig] = None
    mqtt_bearer_override: Optional[MqttBearerOverrideConfig] = None

def load_toml_config(filepath: Path, config_model: Type[T]) -> T:
    """Parses and strictly validates the TOML environment file."""
    if not filepath.exists():
        logger.critical('fatal_mes_environment_configuration_missing_at_filepath', filepath=filepath)
        sys.exit(1)
    try:
        with open(filepath, 'rb') as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        logger.critical('fatal_syntax_error_in_toml_file_name_e', name=filepath.name, e=e)
        sys.exit(1)
    try:
        return config_model.model_validate(data)
    except ValidationError as e:
        print('\n' + '=' * 60)
        print(f'[CONFIG FATAL] Validation failed for {filepath.name}')
        print('=' * 60)
        for err in e.errors():
            location = ' -> '.join(map(str, err['loc']))
            print(f'Location : [{location}]')
            print(f"Error    : {err['msg']}")
            print(f"Input    : {err.get('input', 'N/A')}")
            print('-' * 60)
        print('Please correct the TOML configuration and restart the test session.\n')
        sys.exit(1)