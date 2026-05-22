import anyio
import structlog
import time
import logging
from typing import Any, Optional, List

class _DummyLine:

    def request(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def async_request(self, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.request, *args, **kwargs)

    def set_value(self, value: int) -> None:
        pass

    async def async_set_value(self, value, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.set_value, value, *args, **kwargs)

    def release(self) -> None:
        pass

    async def async_release(self, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.release, *args, **kwargs)

class _DummyChip:

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_line(self, offset: int) -> _DummyLine:
        return _DummyLine()

    async def async_get_line(self, offset, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.get_line, offset, *args, **kwargs)

    def close(self) -> None:
        pass

    async def async_close(self, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.close, *args, **kwargs)

class _DummyGpiod:
    LINE_REQ_DIR_OUT: int = 2
    Chip = _DummyChip
try:
    import gpiod
    HAS_GPIOD = True
except ImportError:
    HAS_GPIOD = False
    gpiod = _DummyGpiod()
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

    async def async_set_boot_mode(self, mode_name, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.set_boot_mode, mode_name, *args, **kwargs)

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
        chip: Optional[Any] = None
        b_lines: List[Any] = []
        r_line: Optional[Any] = None
        try:
            logger.debug('binding_to_gpio_chip_gpiochip', gpiochip=self.cfg.gpiochip)
            chip = gpiod.Chip(f'gpiochip{self.cfg.gpiochip}')
            for i, pin in enumerate(self.cfg.boot_pins):
                logger.debug('acquiring_lock_on_boot_pin_pin', pin=pin)
                line = chip.get_line(pin)
                line.request(consumer=f'mes_boot_{i}', type=gpiod.LINE_REQ_DIR_OUT)
                b_lines.append(line)
            logger.debug('acquiring_lock_on_reset_pin_reset_pin', reset_pin=self.cfg.reset_pin)
            r_line = chip.get_line(self.cfg.reset_pin)
            r_line.request(consumer='mes_reset', type=gpiod.LINE_REQ_DIR_OUT)
            logger.debug('asserting_boot_pins_to_states_target_states', target_states=target_states)
            for line, state in zip(b_lines, target_states):
                line.set_value(state)
            reset_assert_val = 0 if self.cfg.reset_active_low else 1
            reset_release_val = 1 if self.cfg.reset_active_low else 0
            logger.debug('asserting_reset_line_value_reset_assert_val', reset_assert_val=reset_assert_val)
            r_line.set_value(reset_assert_val)
            time.sleep(0.1)
            logger.debug('releasing_reset_line_value_reset_release_val_silicon_sampling_boot_pins_now', reset_release_val=reset_release_val)
            r_line.set_value(reset_release_val)
            time.sleep(0.5)
        except (KeyboardInterrupt, Exception) as e:
            if isinstance(e, KeyboardInterrupt):
                err_msg = 'Bootstrap GPIO sequencing interrupted by operator (Ctrl+C).'
            else:
                err_msg = f'Failed to toggle physical bootstrap pins: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        finally:
            logger.debug('[Bootstrap] ZERO-LEAKAGE: Releasing GPIO locks back to OS.')
            for line in b_lines:
                try:
                    line.release()
                except Exception:
                    pass
            if r_line:
                try:
                    r_line.release()
                except Exception:
                    pass
            if chip:
                try:
                    chip.close()
                except Exception:
                    pass

    def force_recovery_mode(self) -> None:
        """Convenience wrapper to force the silicon into 'recovery' mode."""
        self.set_boot_mode('recovery')

    async def async_force_recovery_mode(self, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.force_recovery_mode, *args, **kwargs)

    def force_normal_boot(self) -> None:
        """Convenience wrapper to force the silicon into 'normal' or 'emmc' mode."""
        mode = 'emmc' if 'emmc' in self.cfg.boot_modes else 'normal'
        self.set_boot_mode(mode)
    async def async_force_normal_boot(self, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.force_normal_boot, *args, **kwargs)
