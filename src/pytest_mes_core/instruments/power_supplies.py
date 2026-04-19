# src/pytest_mes_core/instruments/power_supplies.py
import time
import pyvisa # type: ignore
import logging
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_fixed
from typing import Optional, Any

from pytest_mes_core.config import PsuVendorConfig, RigolPsuConfig, KeysightPsuConfig

logger = logging.getLogger("mes_core.instruments.psu")

# ==========================================
# DOMAIN EXCEPTIONS (Instrument Faults)
# ==========================================
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
    def __init__(self, cfg: PsuVendorConfig, visa_backend: str = ""):
        self.cfg = cfg
        self.rm = pyvisa.ResourceManager(visa_backend)
        self.instrument: Optional[pyvisa.Resource] = None
        self._channel = 1

        # Resolve VISA Resource String based on TOML Discriminated Union
        if isinstance(self.cfg, RigolPsuConfig):
            self.resource_str = f"TCPIP0::{self.cfg.ip_address}::INSTR"
            self._channel = self.cfg.channel
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.resource_str = self.cfg.visa_resource

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1.0), reraise=True)
    def connect(self) -> None:
        logger.debug(f"[PSU] Attempting SCPI connection to {self.resource_str}...")
        try:
            self.instrument = self.rm.open_resource(self.resource_str)
            self.instrument.timeout = 2000 # 2 second timeout for SCPI commands

            # Clear status and verify identity
            self.write("*CLS")
            idn = self.instrument.query("*IDN?").strip()
            logger.info(f"[PSU] Connected successfully to: {idn}")

            # Ensure output is OFF upon connection for safety
            self.disable_output()
        except pyvisa.VisaIOError as e:
            logger.critical("="*60)
            logger.critical(f"[PSU] FATAL: Failed to connect to Power Supply at {self.resource_str}!")
            logger.critical(f"[PSU] Is the instrument powered on? Is the Ethernet cable connected?")
            logger.critical(f"[PSU] VISA Error: {e}")
            logger.critical("="*60)
            raise InstrumentConnectionError(f"FATAL: Power Supply at {self.resource_str} unreachable.")

    def write(self, cmd: str) -> None:
        if not self.instrument: return
        logger.debug(f"[SCPI] TX -> {cmd}")
        self.instrument.write(cmd)

    def query_float(self, cmd: str) -> float:
        if not self.instrument: return 0.0
        try:
            logger.debug(f"[SCPI] TX -> {cmd}")
            raw_response = self.instrument.query(cmd).strip()
            logger.debug(f"[SCPI] RX <- {raw_response}")
            return float(raw_response)
        except ValueError:
            logger.error(f"[PSU] Failed to cast SCPI response to float for cmd: {cmd}")
            return -1.0

    # --- Unified Dialect Commands ---
    def set_voltage(self, volts: float) -> None:
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(f":INST CH{self._channel}")
            self.write(f":VOLT {volts:.3f}")
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.write(f"SOUR:VOLT {volts:.3f},(@{self._channel})")

    def set_current_limit(self, amps: float) -> None:
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(f":INST CH{self._channel}")
            self.write(f":CURR {amps:.3f}")
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.write(f"SOUR:CURR {amps:.3f},(@{self._channel})")

    def enable_output(self) -> None:
        logger.warning("="*60)
        logger.warning(f"[PSU]  DANGER: ENERGIZING OUTPUT ON CHANNEL {self._channel}! ")
        logger.warning("="*60)
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(f":OUTP CH{self._channel},ON")
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.write(f"OUTP ON,(@{self._channel})")

    def disable_output(self) -> None:
        logger.debug(f"[PSU] ZERO-LEAKAGE: Disabling output on channel {self._channel}.")
        if self.instrument:
            if isinstance(self.cfg, RigolPsuConfig):
                self.write(f":OUTP CH{self._channel},OFF")
            elif isinstance(self.cfg, KeysightPsuConfig):
                self.write(f"OUTP OFF,(@{self._channel})")

    def measure_current(self) -> float:
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(f":INST CH{self._channel}")
            return self.query_float(":MEAS:CURR?")
        elif isinstance(self.cfg, KeysightPsuConfig):
            return self.query_float(f"MEAS:CURR? (@{self._channel})")

    def start_data_logger(self) -> None:
        """Configures and starts the hardware-side datalogger."""
        if not hasattr(self.cfg, "enable_data_logging") or not self.cfg.enable_data_logging:
            return
            
        logger.info(f"[PSU] Initiating hardware-side data logger (Interval: {self.cfg.log_interval_s}s)")
        if isinstance(self.cfg, RigolPsuConfig):
            self.write(":MEM:STAT:REC:ENAB ON")
        elif isinstance(self.cfg, KeysightPsuConfig):
            self.write(f"SENS:DLOG:FUNC:VOLT ON,(@{self._channel})")
            self.write(f"SENS:DLOG:FUNC:CURR ON,(@{self._channel})")
            self.write(f"SENS:DLOG:TIME {self.cfg.log_interval_s}")
            self.write("INIT:DLOG")

    def download_data_log(self, export_dir: Path) -> Optional[Path]:
        """Stops the datalogger and retrieves the recorded CSV/Binary payload."""
        if not hasattr(self.cfg, "enable_data_logging") or not self.cfg.enable_data_logging:
            return None
            
        logger.info("[PSU] Transferring hardware data log...")
        log_data = ""
        
        try:
            if isinstance(self.cfg, RigolPsuConfig):
                self.write(":MEM:STAT:REC:ENAB OFF")
                if self.instrument:
                    log_data = self.instrument.query(":MEM:STAT:REC:DATA?")
            elif isinstance(self.cfg, KeysightPsuConfig):
                if self.instrument:
                    v_data = self.instrument.query(f"FETC:DLOG:VOLT? (@{self._channel})")
                    i_data = self.instrument.query(f"FETC:DLOG:CURR? (@{self._channel})")
                    log_data = f"Voltage,Current\n{v_data}\n{i_data}"
        except Exception as e:
            logger.error(f"[PSU] Failed to transfer data log: {e}")
            return None
                
        if log_data:
            export_dir.mkdir(parents=True, exist_ok=True)
            log_path = export_dir / f"psu_datalog_{self.cfg.vendor}_{int(time.time())}.csv"
            try:
                with open(log_path, "w") as f:
                    f.write(log_data)
                logger.info(f"[PSU] Data log saved to {log_path}")
                return log_path
            except IOError as e:
                logger.error(f"[PSU] Failed to write datalog to disk: {e}")
        return None

    def close(self) -> None:
        if self.instrument:
            logger.debug("[PSU] ZERO-LEAKAGE: Closing SCPI VISA session.")
            self.disable_output()
            self.instrument.close()

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
        logger.info(f"[PSU Control] Initiating safe power ramp to {self.target_v}V ({self.current_limit_a}A limit)")

        # 1. Set hard physical limits first
        self.psu.set_current_limit(self.current_limit_a)
        self.psu.set_voltage(0.0)
        self.psu.enable_output()

        # 2. Execute Voltage Ramp (Pre-charge capacitors)
        # Ramp in 1V increments every 50ms to prevent OCP trips
        logger.debug("[PSU Control] Ramping voltage to pre-charge DUT decoupling capacitors...")
        steps = int(self.target_v)
        for v in range(1, steps + 1):
            self.psu.set_voltage(float(v))
            time.sleep(0.05)

        # 3. Set final precise target
        self.psu.set_voltage(self.target_v)
        time.sleep(0.2) # Final settling time

        # 4. Verify no shorts occurred during ramp
        idle_current = self.psu.measure_current()
        logger.info(f"[PSU Control] Ramp complete. Idle current draw: {idle_current}A")

        if idle_current >= (self.current_limit_a * 0.95):
            self.psu.disable_output()
            #  FORENSIC HARDWARE INTERCEPTOR
            logger.critical("="*60)
            logger.critical(f"[PSU Control] FATAL: HARDWARE SHORT CIRCUIT DETECTED!")
            logger.critical(f"[PSU Control] Board pulled {idle_current}A at idle (Limit: {self.current_limit_a}A).")
            logger.critical(f"[PSU Control] Power severed. Check PCB for solder bridges or reversed polarity components.")
            logger.critical("="*60)
            raise InstrumentShortCircuitError(f"FATAL: Board acting as a short circuit! Drew {idle_current}A at idle.")

        return self.psu

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Always kill the power, no exceptions."""
        logger.info("[PSU Control] Test context exiting. De-energizing board.")
        self.psu.disable_output()
