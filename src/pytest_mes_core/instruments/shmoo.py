import time
import logging
from typing import Generator
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply

logger = logging.getLogger("mes_core.instruments.shmoo")

class LiveVoltageSweeper:
    """
    Live-Runtime Parametric Sweeper.
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
        logger.info(f"[Shmoo] Energizing nominal safe boot voltage: {self.nominal_v}V")
        self.psu.set_protection(voltage_max=24.0, current_max=self.current_limit_a)
        self.psu.set_output(voltage=self.nominal_v, current=self.current_limit_a)
        self.psu.enable_output(True)
        self._is_active = True

        # Give the PSU hardware relays time to settle
        time.sleep(0.5)
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
        """
        if not self._is_active:
            raise RuntimeError("Cannot sweep: PSU output is not active.")

        logger.info(f"[Shmoo] Beginning Parametric Sweep: {v_start}V -> {v_end}V (Step: {step_v}V)")

        current_v = v_start
        direction = 1 if v_end > v_start else -1
        step_v = abs(step_v) * direction

        # Force the PSU to the starting voltage
        self.psu.set_output(voltage=current_v)
        time.sleep(settling_time_s)

        # Loop until we cross the v_end boundary
        while (direction > 0 and current_v <= v_end) or (direction < 0 and current_v >= v_end):
            # 1. Command the PSU hardware
            self.psu.set_output(voltage=round(current_v, 3))

            # 2. Wait for copper capacitance to settle
            time.sleep(settling_time_s)

            # 3. Yield control back to the Pytest logic
            yield round(current_v, 3)

            # 4. Advance the step
            current_v += step_v

    def __exit__(self, exc_type, exc_val, exc_tb):
        """ZERO-LEAKAGE: Always kill power when the sweep finishes or the board crashes."""
        logger.info("[Shmoo] Sweep complete/aborted. Discharging power supply...")
        self.psu.set_output(voltage=0.0, current=0.1)
        self.psu.enable_output(False)
        self._is_active = False

        if exc_type:
            logger.warning(f"[Shmoo] Interrupted by exception during sweep: {exc_type.__name__}")
