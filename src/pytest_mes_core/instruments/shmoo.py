import functools
import structlog
import time
import logging
from typing import Generator, Any, AsyncGenerator
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply, InstrumentError
logger = structlog.get_logger('mes_core.instruments.shmoo')

class LiveVoltageSweeper:
    """
    Live-Runtime Parametric Sweeper (Shmoo Plotting).
    Safely boots the DUT, then allows the test to dynamically step voltages
    while searching for hardware brown-outs or thermal runaway.
    """

    def __init__(self, psu: ScpiPowerSupply, nominal_v: float=12.0, current_limit_a: float=3.0):
        self.psu = psu
        self.nominal_v = nominal_v
        self.current_limit_a = current_limit_a
        self._is_active = False

    def __enter__(self) -> 'LiveVoltageSweeper':
        logger.warning('=' * 60)
        logger.warning('danger_energizing_hardware_for_parametric_sweep')
        logger.warning('nominal_boot_voltage_nominal_v_v_current_limit_current_limit_a_a', nominal_v=self.nominal_v, current_limit_a=self.current_limit_a)
        logger.warning('=' * 60)
        self.psu.set_current_limit(self.current_limit_a)
        self.psu.set_voltage(self.nominal_v)
        self.psu.enable_output()
        self._is_active = True
        logger.debug('allowing_2_0s_for_dut_to_boot_and_decouple_capacitors')
        time.sleep(2.0)
        return self

    def sweep(self, v_start: float, v_end: float, step_v: float, settling_time_s: float=0.5) -> Generator[float, None, None]:
        """
        Yields the current voltage step.
        Automatically determines if it should step UP or DOWN.
        Resistant to Python floating-point drift.
        """
        if not self._is_active:
            raise InstrumentError('FATAL: Cannot sweep. PSU output is not actively energized.')
        direction = 1 if v_end >= v_start else -1
        step_v = abs(step_v) * direction
        logger.info('executing_parametric_sweep_v_start_v_v_end_v_step_step_v_v', v_start=v_start, v_end=v_end, step_v=step_v)
        current_v = round(v_start, 3)
        while direction > 0 and current_v <= v_end or (direction < 0 and current_v >= v_end):
            logger.debug('stepping_voltage_to_current_v_v', current_v=current_v)
            self.psu.set_voltage(current_v)
            time.sleep(settling_time_s)
            yield current_v
            current_v = round(current_v + step_v, 3)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Always kill power when the sweep finishes or the board crashes."""
        logger.debug('[Shmoo] Sweep context exiting. Initiating Zero-Leakage teardown...')
        if exc_type:
            logger.critical('=' * 60)
            logger.critical('fatal_hardware_crash_brownout_detected')
            logger.critical('sweep_interrupted_by_exception_name', __name__=exc_type.__name__)
            logger.critical('details_exc_val', exc_val=exc_val)
            logger.critical('the_dut_likely_dropped_off_the_network_because_the_pmic_starved')
            logger.critical('=' * 60)
        else:
            logger.info('[Shmoo] Sweep completed successfully without crashing the DUT.')
        logger.info('[Shmoo] Discharging power supply...')
        self.psu.set_voltage(0.0)
        self.psu.disable_output()
        self._is_active = False
