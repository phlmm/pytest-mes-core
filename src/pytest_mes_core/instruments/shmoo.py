# src/pytest_mes_core/instruments/shmoo.py
import time
import logging
from typing import Generator, Any

from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply

logger = logging.getLogger("mes_core.instruments.shmoo")

class LiveVoltageSweeper:
    """
    Live-Runtime Parametric Sweeper (Shmoo Plotting).
    Safely boots the DUT, then allows the test to dynamically step voltages
    while searching for hardware brown-outs or thermal runaway.
    """
    def __init__(
        self,
        psu: ScpiPowerSupply,
        nominal_v: float = 12.0,
        current_limit_a: float = 3.0
    ):
        self.psu = psu
        self.nominal_v = nominal_v
        self.current_limit_a = current_limit_a
        self._is_active = False

    def __enter__(self) -> 'LiveVoltageSweeper':
        logger.warning("="*60)
        logger.warning(f"[Shmoo]  DANGER: ENERGIZING HARDWARE FOR PARAMETRIC SWEEP ")
        logger.warning(f"[Shmoo] Nominal Boot Voltage: {self.nominal_v}V | Current Limit: {self.current_limit_a}A")
        logger.warning("="*60)

        # Aligned with the ScpiPowerSupply API Contract
        self.psu.set_current_limit(self.current_limit_a)
        self.psu.set_voltage(self.nominal_v)
        self.psu.enable_output()
        self._is_active = True

        # Give the PSU hardware relays and DUT capacitors time to settle
        logger.debug(f"[Shmoo] Allowing 2.0s for DUT to boot and decouple capacitors...")
        time.sleep(2.0)
        return self

    def sweep(
        self,
        v_start: float,
        v_end: float,
        step_v: float,
        settling_time_s: float = 0.5
    ) -> Generator[float, None, None]:
        """
        Yields the current voltage step.
        Automatically determines if it should step UP or DOWN.
        Resistant to Python floating-point drift.
        """
        if not self._is_active:
            raise RuntimeError("FATAL: Cannot sweep. PSU output is not actively energized.")

        direction = 1 if v_end >= v_start else -1
        step_v = abs(step_v) * direction

        logger.info(f"[Shmoo] Executing Parametric Sweep: {v_start}V -> {v_end}V (Step: {step_v}V)")

        current_v = round(v_start, 3)

        # Loop until we cross the v_end boundary
        while (direction > 0 and current_v <= v_end) or (direction < 0 and current_v >= v_end):
            # 1. Command the PSU hardware
            logger.debug(f"[Shmoo] Stepping voltage to {current_v}V...")
            self.psu.set_voltage(current_v)

            # 2. Wait for copper capacitance and PMIC to settle
            time.sleep(settling_time_s)

            # 3. Yield control back to the Pytest logic
            yield current_v

            # 4. Advance the step safely (avoiding float drift like 11.900000000000002)
            current_v = round(current_v + step_v, 3)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Always kill power when the sweep finishes or the board crashes."""
        logger.debug("[Shmoo] Sweep context exiting. Initiating Zero-Leakage teardown...")

        #  FORENSIC BROWNOUT INTERCEPTOR
        if exc_type:
            logger.critical("="*60)
            logger.critical(f"[Shmoo] FATAL: HARDWARE CRASH / BROWNOUT DETECTED!")
            logger.critical(f"[Shmoo] Sweep interrupted by Exception: {exc_type.__name__}")
            logger.critical(f"[Shmoo] Details: {exc_val}")
            logger.critical(f"[Shmoo] The DUT likely dropped off the network because the PMIC starved.")
            logger.critical("="*60)
        else:
            logger.info("[Shmoo] Sweep completed successfully without crashing the DUT.")

        logger.info("[Shmoo] Discharging power supply...")
        # Aligned with the ScpiPowerSupply API Contract
        self.psu.set_voltage(0.0)
        self.psu.disable_output()
        self._is_active = False
