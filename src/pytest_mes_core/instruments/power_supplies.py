import functools
import structlog
import time
import pyvisa
import logging
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_fixed
from typing import Optional, Any
from functools import partial
from pytest_mes_core.config import PsuVendorConfig, RigolPsuConfig, KeysightPsuConfig
logger = structlog.get_logger('mes_core.instruments.psu')

class InstrumentError(Exception):
    """Root exception for all Host PC instrument failures (PSU, DMM, etc.)."""
    pass

class InstrumentConnectionError(InstrumentError):
    """Raised when the VISA/SCPI connection to an instrument fails."""
    pass

class InstrumentShortCircuitError(InstrumentError):
    """Raised when the DUT draws excessive current, indicating a hardware short."""
    pass

class ScpiPowerSupply:
    """
    Unified SCPI driver for COTS Power Supplies.
    Abstracts vendor-specific dialects into a single, clean API.
    """

    def __init__(self, cfg: PsuVendorConfig, visa_backend: str=''):
        self.cfg = cfg
        self.rm = pyvisa.ResourceManager(visa_backend)
        self.instrument: Optional[pyvisa.Resource] = None
        self._channel = 1
        if isinstance(self.cfg, RigolPsuConfig):
            self.resource_str = f'TCPIP0::{self.cfg.ip_address}::INSTR'
            self._channel = self.cfg.channel
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.resource_str = self.cfg.visa_resource

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1.0), reraise=True)
    def connect(self) -> None:
        logger.debug('attempting_scpi_connection_to_resource_str', resource_str=self.resource_str)
        try:
            self.instrument = self.rm.open_resource(self.resource_str)
            self.instrument.timeout = 2000
            self.write('*CLS')
            idn = self.instrument.query('*IDN?').strip()
            logger.info('connected_successfully_to_idn', idn=idn)
            self.disable_output()
        except pyvisa.VisaIOError as e:
            logger.critical('=' * 60)
            logger.critical('fatal_failed_to_connect_to_power_supply_at_resource_str', resource_str=self.resource_str)
            logger.critical('is_the_instrument_powered_on_is_the_ethernet_cable_connected')
            logger.critical('visa_error_e', e=e)
            logger.critical('=' * 60)
            raise InstrumentConnectionError(f'FATAL: Power Supply at {self.resource_str} unreachable.')

    def write(self, cmd: str) -> None:
        if not self.instrument:
            return
        logger.debug('tx_cmd', cmd=cmd)
        self.instrument.write(cmd)

    def query_float(self, cmd: str) -> float:
        if not self.instrument:
            return 0.0
        try:
            logger.debug('tx_cmd', cmd=cmd)
            raw_response = self.instrument.query(cmd).strip()
            logger.debug('rx_raw_response', raw_response=raw_response)
            return float(raw_response)
        except ValueError:
            logger.error('failed_to_cast_scpi_response_to_float_for_cmd_cmd', cmd=cmd)
            return -1.0

    def set_voltage(self, volts: float) -> None:
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(f':INST CH{self._channel}')
            self.write(f':VOLT {volts:.3f}')
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.write(f'SOUR:VOLT {volts:.3f},(@{self._channel})')

    def set_current_limit(self, amps: float) -> None:
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(f':INST CH{self._channel}')
            self.write(f':CURR {amps:.3f}')
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.write(f'SOUR:CURR {amps:.3f},(@{self._channel})')

    def enable_output(self) -> None:
        logger.warning('=' * 60)
        logger.warning('danger_energizing_output_on_channel_channel', _channel=self._channel)
        logger.warning('=' * 60)
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(f':OUTP CH{self._channel},ON')
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.write(f'OUTP ON,(@{self._channel})')

    def disable_output(self) -> None:
        logger.debug('zero_leakage_disabling_output_on_channel_channel', _channel=self._channel)
        if self.instrument:
            if isinstance(self.cfg, RigolPsuConfig):
                self.write(f':OUTP CH{self._channel},OFF')
            elif isinstance(self.cfg, KeysightPsuConfig):
                self.write(f'OUTP OFF,(@{self._channel})')

    def measure_current(self) -> float:
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(f':INST CH{self._channel}')
            return self.query_float(':MEAS:CURR?')
        elif isinstance(self.cfg, KeysightPsuConfig):
            return self.query_float(f'MEAS:CURR? (@{self._channel})')

    def measure_voltage(self) -> float:
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(f':INST CH{self._channel}')
            return self.query_float(':MEAS:VOLT?')
        elif isinstance(self.cfg, KeysightPsuConfig):
            return self.query_float(f'MEAS:VOLT? (@{self._channel})')

    def start_data_logger(self) -> None:
        """Configures and starts the hardware-side datalogger."""
        if not hasattr(self.cfg, 'enable_data_logging') or not self.cfg.enable_data_logging:
            return
        logger.info('initiating_hardware_side_data_logger_interval_log_interval_s_s', log_interval_s=self.cfg.log_interval_s)
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(':MEM:STAT:REC:ENAB ON')
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.write(f'SENS:DLOG:FUNC:VOLT ON,(@{self._channel})')
            self.write(f'SENS:DLOG:FUNC:CURR ON,(@{self._channel})')
            self.write(f'SENS:DLOG:TIME {self.cfg.log_interval_s}')
            self.write('INIT:DLOG')

    def download_data_log(self, export_dir: Path) -> Optional[Path]:
        """Stops the datalogger and retrieves the recorded CSV/Binary payload."""
        if not hasattr(self.cfg, 'enable_data_logging') or not self.cfg.enable_data_logging:
            return None
        logger.info('[PSU] Transferring hardware data log...')
        log_data = ''
        try:
            if isinstance(self.cfg, RigolPsuConfig):
                self.write(':MEM:STAT:REC:ENAB OFF')
                if self.instrument:
                    log_data = self.instrument.query(':MEM:STAT:REC:DATA?')
            elif isinstance(self.cfg, KeysightPsuConfig):
                if self.instrument:
                    v_data = self.instrument.query(f'FETC:DLOG:VOLT? (@{self._channel})')
                    i_data = self.instrument.query(f'FETC:DLOG:CURR? (@{self._channel})')
                    log_data = f'Voltage,Current\n{v_data}\n{i_data}'
        except Exception as e:
            logger.error('failed_to_transfer_data_log_e', e=e)
            return None
        if log_data:
            export_dir.mkdir(parents=True, exist_ok=True)
            log_path = export_dir / f'psu_datalog_{self.cfg.vendor}_{int(time.time())}.csv'
            try:
                with open(log_path, 'w') as f:
                    f.write(log_data)
                logger.info('data_log_saved_to_log_path', log_path=log_path)
                return log_path
            except IOError as e:
                logger.error('failed_to_write_datalog_to_disk_e', e=e)
        return None

    def close(self) -> None:
        if self.instrument:
            logger.debug('[PSU] ZERO-LEAKAGE: Closing SCPI VISA session.')
            self.disable_output()
            self.instrument.close()

    # ------------------------------------------------------------------
    # Async API (anyio-compatible)
    # ------------------------------------------------------------------











class SafePowerController:
    """
    Defends against capacitive inrush currents by ramping voltage.
    Guarantees output de-energization upon test completion, failure, or E-Stop.
    """

    def __init__(self, psu: ScpiPowerSupply, target_v: float, current_limit_a: float):
        self.psu = psu
        self.target_v = target_v
        self.current_limit_a = current_limit_a

    def __enter__(self) -> ScpiPowerSupply:
        logger.info('initiating_safe_power_ramp_to_target_v_v_current_limit_a_a_limit', target_v=self.target_v, current_limit_a=self.current_limit_a)
        self.psu.set_current_limit(self.current_limit_a)
        self.psu.set_voltage(0.0)
        self.psu.enable_output()
        logger.debug('[PSU Control] Ramping voltage to pre-charge DUT decoupling capacitors...')
        steps = int(self.target_v)
        for v in range(1, steps + 1):
            self.psu.set_voltage(float(v))
            time.sleep(0.05)
        self.psu.set_voltage(self.target_v)
        time.sleep(0.2)
        idle_current = self.psu.measure_current()
        logger.info('ramp_complete_idle_current_draw_idle_current_a', idle_current=idle_current)
        if idle_current >= self.current_limit_a * 0.95:
            self.psu.disable_output()
            logger.critical('=' * 60)
            logger.critical('fatal_hardware_short_circuit_detected')
            logger.critical('board_pulled_idle_current_a_at_idle_limit_current_limit_a_a', idle_current=idle_current, current_limit_a=self.current_limit_a)
            logger.critical('power_severed_check_pcb_for_solder_bridges_or_reversed_polarity_components')
            logger.critical('=' * 60)
            raise InstrumentShortCircuitError(f'FATAL: Board acting as a short circuit! Drew {idle_current}A at idle.')
        return self.psu

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Always kill the power, no exceptions."""
        logger.info('[PSU Control] Test context exiting. De-energizing board.')
        self.psu.disable_output()
