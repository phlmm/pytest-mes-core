import structlog
import time
import logging
from typing import List

try:
    import gpiod
    HAS_GPIOD = True
except ImportError:
    HAS_GPIOD = False
    gpiod = None
from pytest_mes_core.config import BootstrapConfig
from pytest_mes_core.provisioning.base import ProvisioningError
logger = structlog.get_logger('mes_core.provisioning.bootstrap')

class HardwareBootstrapper:
    """
    Manipulates up to 4 physical Host PC GPIOs connected to the DUT's Boot Mode pins.
    Forces complex SoCs into specific states (eMMC, USB Recovery, SD Card) dynamically.
    """

    def __init__(self, cfg: BootstrapConfig):
        self.cfg = cfg

    def set_boot_mode(self, mode_name: str) -> None:
        """Public method to dynamically assert any boot state defined in the station configuration.

        Args:
            mode_name: The string identifier of the boot mode (e.g., 'recovery', 'emmc').

        Raises:
            ProvisioningError: If the mode is undefined or has an incorrect number of pin states.
        """
        if mode_name not in self.cfg.boot_modes:
            err_msg = f"Boot mode '{mode_name}' is not defined in the station configuration."
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        target_states = self.cfg.boot_modes[mode_name]
        if len(target_states) != len(self.cfg.boot_pins):
            err_msg = f"Mismatch: Mode '{mode_name}' provides {len(target_states)} states, but {len(self.cfg.boot_pins)} boot pins are configured."
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        logger.info('forcing_silicon_into_val_mode_states_target_states', val=mode_name.upper(), target_states=target_states)
        self._strobe_hardware(target_states)


    def _strobe_hardware(self, target_states: List[int]) -> None:
        """Internal helper to assert multiplexed boot pins and strobe the reset line.

        Args:
            target_states: A list of binary integers (0 or 1) representing the target
                state for each configured boot pin.

        Raises:
            ProvisioningError: If the physical GPIO toggling fails.
        """
        if not HAS_GPIOD:
            logger.warning('[Bootstrap] gpiod missing. Hardware boot state bypassed! (OK if testing on Windows/Mac)')
            return
        from gpiod.line import Direction, Value  # local import next to usage

        try:
            chip_path = f'/dev/gpiochip{self.cfg.gpiochip}'
            pins = list(self.cfg.boot_pins) + [self.cfg.reset_pin]
            logger.debug('requesting_gpio_lines_on_chip_path_for_pins', chip_path=chip_path, pins=pins)
            config = {pin: gpiod.LineSettings(direction=Direction.OUTPUT) for pin in pins}
            with gpiod.request_lines(chip_path, consumer='mes_bootstrap', config=config) as request:
                logger.debug('asserting_boot_pins_to_states_target_states', target_states=target_states)
                for pin, state in zip(self.cfg.boot_pins, target_states):
                    request.set_value(pin, Value.ACTIVE if state else Value.INACTIVE)
                reset_assert = Value.INACTIVE if self.cfg.reset_active_low else Value.ACTIVE
                reset_release = Value.ACTIVE if self.cfg.reset_active_low else Value.INACTIVE
                logger.debug('asserting_reset_line_value_reset_assert', reset_assert=reset_assert)
                request.set_value(self.cfg.reset_pin, reset_assert)
                time.sleep(0.1)
                logger.debug('releasing_reset_line_value_reset_release_silicon_sampling_boot_pins_now', reset_release=reset_release)
                request.set_value(self.cfg.reset_pin, reset_release)
                time.sleep(0.5)
        except (KeyboardInterrupt, Exception) as e:
            if isinstance(e, KeyboardInterrupt):
                err_msg = 'Bootstrap GPIO sequencing interrupted by operator (Ctrl+C).'
            else:
                err_msg = f'Failed to toggle physical bootstrap pins: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)

    def force_recovery_mode(self) -> None:
        """Convenience wrapper to force the silicon into 'recovery' mode."""
        self.set_boot_mode('recovery')


    def force_normal_boot(self) -> None:
        """Convenience wrapper to force the silicon into 'normal' or 'emmc' mode."""
        mode = 'emmc' if 'emmc' in self.cfg.boot_modes else 'normal'
        self.set_boot_mode(mode)
