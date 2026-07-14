import structlog
# src/pytest_mes_core/host_adapters/sd_mux.py
import os
import logging
import subprocess
import sys
import time
from typing import Any, Optional
from pytest_mes_core.config import UsbSdMuxConfig
from pytest_mes_core.host_adapters import BaseHostAdapter, HostAdapterError
from pytest_mes_core.host_adapters import hardware_mutex, HostMutexTimeoutError

logger = structlog.get_logger('mes_core.host_adapters.usb_sd_mux')

# ---------------------------------------------------------------------------
# Optional fast-path: import the usbsdmux Python library for GPIO control.
# The library ships with the `usbsdmux` package (same package that provides
# the CLI tool).  Only the Fast variant (sdFST HS-SD/MMC) exposes user GPIOs;
# the Classic variant silently raises NotImplementedError on gpio_* calls.
# ---------------------------------------------------------------------------
try:
    from usbsdmux.usbsdmux import autoselect_driver as _usbsdmux_autoselect
    _HAS_USBSDMUX_LIB = True
except ImportError:
    _HAS_USBSDMUX_LIB = False
    _usbsdmux_autoselect = None  # type: ignore[assignment]


class HostUsbSdMuxAdapter(BaseHostAdapter):
    """
    Manages the physical hardware state of a Linux Automation USB-SD-Mux.
    Guarantees the SD card is returned to the DUT (Device Under Test) on teardown.

    Fast-variant GPIO (sdFST HS-SD/MMC)
    ------------------------------------
    When ``config.recovery_gpio`` is set to 0 or 1 the adapter can also assert
    and release the DUT recovery line by driving that auxiliary open-drain output
    directly via the ``usbsdmux`` Python library — no separate gpiod/gpiochip
    wiring needed for this signal.

    * ``recovery_assert()`` — drives the configured GPIO **low** (open-drain asserted)
    * ``recovery_release()`` — drives the configured GPIO **high** (released / floating)

    On the Classic variant (Pca9536-based) these methods are no-ops and emit a
    warning, because that hardware has no user GPIOs.
    """

    def __init__(self, config: UsbSdMuxConfig, mutex_timeout_s: float = 60.0):
        self.cfg = config
        self.mutex_timeout_s = mutex_timeout_s
        self._mutex_context = None
        if self.cfg.serial_id.startswith('/dev/'):
            self.device_path = self.cfg.serial_id
        else:
            self.device_path = self._resolve_device_path(self.cfg.serial_id)

    @staticmethod
    def _resolve_device_path(serial_id: str) -> str:
        """Resolve the /dev/usb-sd-mux/id-* symlink for *serial_id*.

        The udev rule creates entries like::

            /dev/usb-sd-mux/id-000000001781   ← Classic (12-digit)
            /dev/usb-sd-mux/id-00048.00717    ← Fast    (dotted, no padding)

        Rather than guessing the padding, we glob the directory and pick the
        entry whose filename ends with the configured serial_id.  If nothing
        matches we fall back to ``/dev/usb-sd-mux/id-<serial_id>`` verbatim
        so the usual "device not found" error is still raised by the caller.
        """
        import glob as _glob
        candidates = _glob.glob('/dev/usb-sd-mux/id-*')
        for path in candidates:
            # Match by suffix so '00048.00717' matches 'id-00048.00717'
            if os.path.basename(path).endswith(serial_id):
                return path
        # Fallback — no udev entry found; let the caller raise the usual error.
        return f'/dev/usb-sd-mux/id-{serial_id}'

    def _set_mux_state(self, state: str) -> None:
        """Helper to invoke the usbsdmux CLI."""
        if state not in ['host', 'dut', 'off']:
            raise ValueError("Mux state must be 'host', 'dut', or 'off'")

        # 1. Pre-flight check: Ensure the udev symlink actually exists in Linux
        if not os.path.exists(self.device_path):
            err_msg = f"USB-SD-Mux not found at '{self.device_path}'. Is it plugged in, and are the udev rules (99-usbsdmux.rules) installed?"
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)

        cmd = ['usbsdmux', self.device_path, state]
        logger.debug('executing_val', val=' '.join(cmd))
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                logger.error('usbsdmux_cli_rejected_the_command_stderr_val', val=res.stderr.strip())
                raise HostAdapterError(f'usbsdmux failed to switch to {state}: {res.stderr.strip()}')
        except FileNotFoundError:
            err_msg = "The 'usbsdmux' tool is not installed on the Host PC."
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)
        except subprocess.TimeoutExpired:
            err_msg = f'usbsdmux timed out switching {self.device_path} to {state}.'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)

    def _open_driver(self) -> Optional[Any]:
        """
        Opens a usbsdmux Python driver handle for whichever variant is connected.

        Works for both Classic (Pca9536) and Fast (Tca6408) variants.
        Returns ``None`` — with a warning — if the library is absent or the
        device path does not exist.
        """
        if not _HAS_USBSDMUX_LIB:
            logger.warning(
                '[SD-Mux] usbsdmux Python library unavailable. '
                'Install the usbsdmux package to enable direct driver access.'
            )
            return None

        if not os.path.exists(self.device_path):
            logger.warning(
                '[SD-Mux] Driver open requested but device not found at device_path',
                device_path=self.device_path,
            )
            return None

        try:
            # autoselect_driver probes the SCSI model string and returns the
            # correct UsbSdMuxClassic or UsbSdMuxFast instance.
            return _usbsdmux_autoselect(self.device_path)
        except Exception as e:
            logger.warning('[SD-Mux] Failed to open usbsdmux driver', error=str(e))
            return None

    def _get_fast_driver(self) -> Optional[Any]:
        """
        Opens a driver handle **only** when recovery GPIO control is configured.

        Returns the driver on success, or ``None`` if:
        - ``recovery_gpio`` is not set in the config (Classic / no-GPIO setup)
        - the ``usbsdmux`` library is not installed
        - the device path does not exist
        """
        if self.cfg.recovery_gpio is None:
            # GPIO control not configured — silent no-op for Classic setups.
            return None
        return self._open_driver()

    def _set_gpio(self, gpio: int, *, high: bool) -> None:
        """
        Drive a Fast-variant auxiliary GPIO high or low.

        Skips silently when the driver cannot be acquired (Classic variant,
        library absent, device offline).
        """
        driver = self._get_fast_driver()
        if driver is None:
            return

        direction = 'HIGH' if high else 'LOW'
        logger.debug(
            'sd_mux_fast_gpio_drive',
            gpio=gpio, direction=direction, device_path=self.device_path,
        )
        try:
            if high:
                driver.gpio_set_high(gpio)
            else:
                driver.gpio_set_low(gpio)
        except NotImplementedError:
            # The Classic variant raises NotImplementedError on gpio_* calls.
            logger.warning(
                '[SD-Mux] GPIO control is only supported on the Fast variant (sdFST). '
                'The connected device appears to be the Classic variant. '
                'Check recovery_gpio setting in station config.',
                device_path=self.device_path,
            )
        except Exception as e:
            raise HostAdapterError(
                f'Failed to drive Fast-variant GPIO {gpio} {direction} on {self.device_path}: {e}'
            ) from e

    # ------------------------------------------------------------------
    # Recovery GPIO public interface
    # ------------------------------------------------------------------

    def recovery_assert(self) -> None:
        """Assert the DUT recovery line via the Fast-variant GPIO (open-drain LOW).

        Drives the GPIO configured in ``config.recovery_gpio`` low, pulling
        the DUT recovery pin to ground so the SoC samples RECOVERY on the
        next reset strobe.

        No-op when ``recovery_gpio`` is not configured or the device is the
        Classic variant.
        """
        if self.cfg.recovery_gpio is None:
            logger.debug('[SD-Mux] recovery_assert: recovery_gpio not configured, skipping.')
            return
        logger.info(
            'sd_mux_recovery_assert',
            gpio=self.cfg.recovery_gpio, device_path=self.device_path,
        )
        self._set_gpio(self.cfg.recovery_gpio, high=False)

    def recovery_release(self) -> None:
        """Release the DUT recovery line via the Fast-variant GPIO (open-drain HIGH).

        Drives the GPIO configured in ``config.recovery_gpio`` high, releasing
        the open-drain pull so the DUT boots normally on subsequent resets.

        No-op when ``recovery_gpio`` is not configured or the device is the
        Classic variant.
        """
        if self.cfg.recovery_gpio is None:
            logger.debug('[SD-Mux] recovery_release: recovery_gpio not configured, skipping.')
            return
        logger.info(
            'sd_mux_recovery_release',
            gpio=self.cfg.recovery_gpio, device_path=self.device_path,
        )
        self._set_gpio(self.cfg.recovery_gpio, high=True)

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    def get_mux_state(self) -> str:
        """Return the current switch state of the SD-Mux.

        Queries the hardware directly via the ``usbsdmux`` Python library.
        Works on both the Classic and Fast variant.

        Returns:
            str: One of ``'host'``, ``'dut'``, or ``'off'``.

        Raises:
            HostAdapterError: If the driver cannot be opened or the query fails.
        """
        driver = self._open_driver()
        if driver is None:
            raise HostAdapterError(
                f"Cannot query mux state: usbsdmux driver unavailable for '{self.device_path}'. "
                "Ensure the device is connected and the usbsdmux package is installed."
            )
        try:
            state = driver.get_mode()
            logger.debug('sd_mux_state_query', state=state, device_path=self.device_path)
            return state
        except Exception as e:
            raise HostAdapterError(
                f"Failed to read mux state from '{self.device_path}': {e}"
            ) from e

    def get_recovery_gpio_state(self) -> Optional[str]:
        """Return the current level of the recovery GPIO on the Fast variant.

        Reads the GPIO configured in ``config.recovery_gpio`` directly from
        the TCA6408 I²C expander on the sdFST hardware.

        Returns:
            ``'high'`` or ``'low'`` when a ``recovery_gpio`` is configured and
            the Fast variant is in use.  Returns ``None`` \u2014 without raising \u2014
            when ``recovery_gpio`` is not configured or the driver is unavailable.

        Raises:
            HostAdapterError: If the driver is available but the read itself fails.
        """
        if self.cfg.recovery_gpio is None:
            logger.debug('[SD-Mux] get_recovery_gpio_state: recovery_gpio not configured.')
            return None

        driver = self._open_driver()
        if driver is None:
            return None

        try:
            level = driver.gpio_get(self.cfg.recovery_gpio)
            logger.debug(
                'sd_mux_gpio_state_query',
                gpio=self.cfg.recovery_gpio, level=level, device_path=self.device_path,
            )
            return level
        except NotImplementedError:
            logger.warning(
                '[SD-Mux] gpio_get not supported on this variant (Classic). '
                'Remove recovery_gpio from the station config if using a Classic device.',
                device_path=self.device_path,
            )
            return None
        except Exception as e:
            raise HostAdapterError(
                f"Failed to read GPIO {self.cfg.recovery_gpio} state from '{self.device_path}': {e}"
            ) from e

    # ------------------------------------------------------------------
    # Context manager (host-lock / zero-leakage teardown)
    # ------------------------------------------------------------------

    def __enter__(self) -> 'HostUsbSdMuxAdapter':
        """Acquires a cross-process lock and toggles the SD Mux to the Host PC.

        Includes a defensive 2-second sleep to ensure the Linux kernel completes
        USB block device enumeration before returning.

        Returns:
            HostUsbSdMuxAdapter: The locked and host-connected SD Mux adapter.

        Raises:
            HostAdapterError: If the OS lock fails or the usbsdmux command fails.
        """
        logger.debug('acquiring_hardware_lock_for_mux_serial_id', serial_id=self.cfg.serial_id)
        try:
            self._mutex_context = hardware_mutex(resource_name=f'sdmux_{self.cfg.serial_id}', timeout_s=self.mutex_timeout_s)
        except HostMutexTimeoutError as e:
            err_msg = f'Failed to acquire SD-Mux {self.cfg.serial_id}: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)

        self._mutex_context.__enter__()
        try:
            # 2. Toggle physical hardware to HOST
            logger.info('hardware_locked_toggling_device_path_to_host_pc', device_path=self.device_path)
            self._set_mux_state('host')

            # 3. Defeat Linux Kernel USB Enumeration Jitter
            # Give the Host PC 2 seconds to enumerate the block device (e.g., /dev/sdc)
            logger.debug('[SD-Mux] Delaying 2.0s for Linux Kernel block device enumeration...')
            time.sleep(2.0)
            return self
        except BaseException:
            self._mutex_context.__exit__(*sys.exc_info())
            self._mutex_context = None
            raise

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Flip the SD card back to the DUT and release the lock."""
        try:
            logger.info('zero_leakage_toggling_device_path_back_to_dut', device_path=self.device_path)
            self._set_mux_state('dut')
            time.sleep(1.0) # Allow DUT to detect insertion
        except Exception as e:
            logger.warning('teardown_hardware_failure_on_serial_id_e', serial_id=self.cfg.serial_id, e=e)
        finally:
            if self._mutex_context:
                logger.debug('zero_leakage_releasing_os_lock_on_serial_id', serial_id=self.cfg.serial_id)
                self._mutex_context.__exit__(_exc_type, _exc_val, _exc_tb)
                self._mutex_context = None


# ---------------------------------------------------------------------------
# RecoveryStrategy implementation backed by the sdFST GPIO
# ---------------------------------------------------------------------------

class SdMuxRecoveryStrategy:
    """
    Drives the DUT into NXP Serial Downloader / USB recovery mode by asserting
    the sdFST Fast-variant GPIO that is wired to the board's RECOVERY# pin.

    Lifecycle
    ---------
    trigger_recovery(fsm)
        1. Assert GPIO (LOW) — holds RECOVERY# before power is applied.
        2. Power-cycle the board via ``fsm._do_energize()``.
        3. Wait ``latch_time_s`` for the SoC BootROM to sample the pin.
        4. Release GPIO (HIGH) — de-asserts RECOVERY# so subsequent resets
           boot normally (e.g. from the eMMC after TEZI flashes it).

    release_recovery(fsm)
        No-op — the pin is already released inside ``trigger_recovery`` after
        the latch delay.  The method exists to satisfy the ``RecoveryStrategy``
        interface so this class is a drop-in replacement for GpioRecoveryStrategy.

    Parameters
    ----------
    mux:
        A ``HostUsbSdMuxAdapter`` whose ``recovery_gpio`` is configured in the
        station TOML.  Raises ``ValueError`` at construction time if
        ``mux.cfg.recovery_gpio`` is ``None``.
    latch_time_s:
        How long to hold the GPIO asserted after power is applied.
        1.0 s is sufficient for the NXP iMX8MP BootROM to sample the pin.
    """

    def __init__(self, mux: 'HostUsbSdMuxAdapter', latch_time_s: float = 1.0) -> None:
        if mux.cfg.recovery_gpio is None:
            raise ValueError(
                "SdMuxRecoveryStrategy requires recovery_gpio to be set in UsbSdMuxConfig. "
                "Add 'recovery_gpio = 0' (or 1) to the [usb_sd_mux.*] section in your station TOML."
            )
        self._mux = mux
        self._latch_time_s = latch_time_s

    async def trigger_recovery(self, fsm: Any) -> None:
        """Assert RECOVERY# via the sdFST GPIO, power-cycle, then release after latch."""
        import anyio
        logger.info(
            'sd_mux_recovery_trigger',
            gpio=self._mux.cfg.recovery_gpio,
            latch_time_s=self._latch_time_s,
        )
        # 0. Switch SD to 'dut' so the SD card is available to the DUT before power is applied.
        logger.info('sd_mux_to_dut_before_boot', device_path=self._mux.device_path)
        await anyio.to_thread.run_sync(self._mux._set_mux_state, 'dut')
        await anyio.sleep(0.3)

        # 1. Assert RECOVERY# — must happen BEFORE power is applied so the
        #    SoC BootROM samples it on the rising edge of VDD.
        await anyio.to_thread.run_sync(self._mux.recovery_assert)

        # 2. Apply power.  _do_energize() reconnects the serial port as well.
        await anyio.to_thread.run_sync(fsm._do_energize)

        # 3. Hold asserted long enough for BootROM to latch the boot mode.
        logger.debug('sd_mux_recovery_latch_wait', latch_time_s=self._latch_time_s)
        await anyio.sleep(self._latch_time_s)

        # 4. Release — RECOVERY# goes high; subsequent resets boot normally.
        await anyio.to_thread.run_sync(self._mux.recovery_release)
        logger.info('sd_mux_recovery_trigger_complete', action='pin_released_dut_enumerating_on_usb')


    async def release_recovery(self, fsm: Any) -> None:
        """No-op — the pin was already released inside trigger_recovery."""
        logger.debug('sd_mux_release_recovery_no_op', reason='pin_released_during_trigger_after_latch')