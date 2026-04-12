# src/pytest_mes_core/instruments/power_supplies.py
import time
import pyvisa # type: ignore
import logging
from tenacity import retry, stop_after_attempt, wait_fixed
from typing import Optional

from pytest_mes_core.config import PsuVendorConfig, RigolPsuConfig, KeysightPsuConfig

logger = logging.getLogger("mes_core.instruments.psu")

class ScpiPowerSupply:
    """
    Unified SCPI driver for COTS Power Supplies.
    Abstracts vendor-specific dialects into a single, clean API.
    """
    def __init__(self, cfg: PsuVendorConfig):
        self.cfg = cfg
        self.rm = pyvisa.ResourceManager()
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
            self.instrument.write("*CLS")
            idn = self.instrument.query("*IDN?").strip()
            logger.info(f"[PSU] Connected successfully to: {idn}")

            # Ensure output is OFF upon connection for safety
            self.disable_output()
        except pyvisa.VisaIOError as e:
            logger.error(f"[PSU] Connection failed. Is the instrument powered on? {e}")
            raise RuntimeError(f"FATAL: Power Supply at {self.resource_str} unreachable.")

    def write(self, cmd: str) -> None:
        if not self.instrument: return
        self.instrument.write(cmd)

    def query_float(self, cmd: str) -> float:
        if not self.instrument: return 0.0
        try:
            return float(self.instrument.query(cmd).strip())
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
        logger.warning(f"[PSU] ENERGIZING OUTPUT ON CHANNEL {self._channel}!")
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

    def close(self) -> None:
        if self.instrument:
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
            raise RuntimeError(f"FATAL: Board acting as a short circuit! Drew {idle_current}A at idle.")

        return self.psu

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """ZERO-LEAKAGE: Always kill the power, no exceptions."""
        logger.info("[PSU Control] Test context exiting. De-energizing board.")
        self.psu.disable_output()
