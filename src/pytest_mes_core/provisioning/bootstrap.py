# src/pytest_mes_core/provisioning/bootstrap.py
import time
import logging
from typing import Any, Optional, List

# ==========================================
# CROSS-PLATFORM STATIC TYPING STUBS
# ==========================================
class _DummyLine:
    def request(self, *args: Any, **kwargs: Any) -> None: pass
    def set_value(self, value: int) -> None: pass
    def release(self) -> None: pass

class _DummyChip:
    def __init__(self, *args: Any, **kwargs: Any) -> None: pass
    def get_line(self, offset: int) -> _DummyLine: return _DummyLine()
    def close(self) -> None: pass

class _DummyGpiod:
    LINE_REQ_DIR_OUT: int = 2
    Chip = _DummyChip

try:
    import gpiod # type: ignore
    HAS_GPIOD = True
except ImportError:
    HAS_GPIOD = False
    gpiod = _DummyGpiod() # type: ignore

from pytest_mes_core.config import BootstrapConfig
from pytest_mes_core.provisioning.base import ProvisioningError

logger = logging.getLogger("mes_core.provisioning.bootstrap")

class HardwareBootstrapper:
    """
    Manipulates up to 4 physical Host PC GPIOs connected to the DUT's Boot Mode pins.
    Forces complex SoCs into specific states (eMMC, USB Recovery, SD Card) dynamically.
    """
    def __init__(self, cfg: BootstrapConfig):
        self.cfg = cfg

    def set_boot_mode(self, mode_name: str) -> None:
        """
        Public method to dynamically assert any boot state defined in the station configuration.
        """
        if mode_name not in self.cfg.boot_modes:
            raise ProvisioningError(
                f"Boot mode '{mode_name}' is not defined in the station configuration."
            )

        target_states = self.cfg.boot_modes[mode_name]

        if len(target_states) != len(self.cfg.boot_pins):
            raise ProvisioningError(
                f"Mismatch: Mode '{mode_name}' provides {len(target_states)} states, "
                f"but {len(self.cfg.boot_pins)} boot pins are configured."
            )

        logger.info(f"[Bootstrap] Forcing silicon into '{mode_name.upper()}' mode (States: {target_states})...")
        self._strobe_hardware(target_states)

    def _strobe_hardware(self, target_states: List[int]) -> None:
        """Internal helper to assert multiplexed boot pins and strobe the reset line."""
        if not HAS_GPIOD:
            logger.warning("[Bootstrap] gpiod missing. Hardware boot state bypassed!")
            return

        # DEFENSIVE: Initialize variables to prevent UnboundLocalError in finally block
        chip: Optional[Any] = None
        b_lines: List[Any] = []
        r_line: Optional[Any] = None

        try:
            chip = gpiod.Chip(f"gpiochip{self.cfg.gpiochip}")

            # Request the exact number of Boot Mode lines
            for i, pin in enumerate(self.cfg.boot_pins):
                line = chip.get_line(pin)
                line.request(consumer=f"mes_boot_{i}", type=gpiod.LINE_REQ_DIR_OUT)
                b_lines.append(line)

            # Request Reset line
            r_line = chip.get_line(self.cfg.reset_pin)
            r_line.request(consumer="mes_reset", type=gpiod.LINE_REQ_DIR_OUT)

            # 1. Assert the multiplexed Boot States
            for line, state in zip(b_lines, target_states):
                line.set_value(state)

            # 2. Assert Reset
            reset_assert_val = 0 if self.cfg.reset_active_low else 1
            reset_release_val = 1 if self.cfg.reset_active_low else 0

            r_line.set_value(reset_assert_val)
            time.sleep(0.1) # Allow silicon capacitors to drain

            # 3. Release Reset (Silicon samples BOOT pins on the rising/falling edge of reset)
            r_line.set_value(reset_release_val)
            time.sleep(0.5) # Wait for Boot ROM to lock in the mode

        except Exception as e:
            raise ProvisioningError(f"Failed to toggle physical bootstrap pins: {e}")

        finally:
            # ZERO-LEAKAGE: Safely release all GPIO lines back to the Linux Kernel
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

    # Convenience Wrappers for standard Pytest Fixtures
    def force_recovery_mode(self) -> None:
        self.set_boot_mode("recovery")

    def force_normal_boot(self) -> None:
        # Fallback to "emmc" if defined, otherwise use "normal"
        mode = "emmc" if "emmc" in self.cfg.boot_modes else "normal"
        self.set_boot_mode(mode)
