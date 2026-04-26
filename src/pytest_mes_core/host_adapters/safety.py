import structlog
import os
import time
import signal
import threading
import logging
from typing import Optional, Any

class _DummyLine:

    def request(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_value(self) -> int:
        return 1

    def release(self) -> None:
        pass

class _DummyChip:

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_line(self, offset: int) -> _DummyLine:
        return _DummyLine()

    def close(self) -> None:
        pass

class _DummyGpiod:
    LINE_REQ_DIR_IN: int = 1
    Chip = _DummyChip
try:
    import gpiod
    HAS_GPIOD = True
except ImportError:
    HAS_GPIOD = False
    gpiod = _DummyGpiod()
from pytest_mes_core.config import EStopConfig
from pytest_mes_core.host_adapters.base import BaseHostAdapter, HostAdapterError
logger = structlog.get_logger('mes_core.host_adapters.safety')

class EStopWatchdog(BaseHostAdapter):
    """
    Runs completely outside the pytest event loop.
    Monitors a physical hardware E-Stop button.
    Triggers a graceful but immediate cascade failure to ensure high-voltage teardowns execute.
    Complies with the BaseHostAdapter Zero-Leakage contract.
    """

    def __init__(self, cfg: EStopConfig):
        self.cfg = cfg
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.chip: Optional[Any] = None
        self.line: Optional[Any] = None

    def __enter__(self) -> 'EStopWatchdog':
        """Binds the GPIO line for the E-Stop button and spawns the monitor thread.

        Returns:
            EStopWatchdog: The armed safety watchdog instance.

        Raises:
            HostAdapterError: If the GPIO chip or line cannot be claimed.
        """
        if not HAS_GPIOD:
            logger.warning('[Safety] gpiod not available. E-Stop bypassed. DANGEROUS IF PHYSICAL HIGH VOLTAGE IS PRESENT!')
            return self
        logger.debug('initializing_gpio_lock_on_host_chip_gpiochip_line_line', gpiochip=self.cfg.gpiochip, line=self.cfg.line)
        try:
            self.chip = gpiod.Chip(f'gpiochip{self.cfg.gpiochip}')
            self.line = self.chip.get_line(self.cfg.line)
            self.line.request(consumer='mes_estop', type=gpiod.LINE_REQ_DIR_IN)
        except Exception as e:
            err_msg = f'Failed to bind E-Stop hardware on chip{self.cfg.gpiochip}:line{self.cfg.line}. Error: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)
        logger.info('[Safety] Watchdog Armed. Physical E-Stop button actively monitored.')
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Disarm the watchdog and release GPIO pins."""
        logger.debug('[Safety] ZERO-LEAKAGE: Disarming E-Stop Watchdog.')
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self.line:
            try:
                self.line.release()
            except Exception as e:
                logger.debug('failed_to_release_line_lock_e', e=e)
        if self.chip:
            try:
                self.chip.close()
            except Exception as e:
                logger.debug('failed_to_close_chip_e', e=e)

    def _monitor(self) -> None:
        """Background thread logic for monitoring physical E-Stop state.
        
        Executes an immediate SIGINT and hard kill if the operator triggers the E-Stop
        or if consecutive I/O read failures occur.
        """
        if not self.line:
            return
        trigger_state = 0 if self.cfg.active_low else 1
        logger.debug('background_poller_started_target_trigger_state_trigger_state', trigger_state=trigger_state)
        MAX_CONSECUTIVE_ERRORS = 3
        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                state = self.line.get_value()
                consecutive_errors = 0
                logger.debug('pin_state_state', state=state)
                if state == trigger_state:
                    logger.critical('=' * 60)
                    logger.critical('FATAL: PHYSICAL E-STOP DEPLOYED! INITIATING EMERGENCY HALT!')
                    logger.critical('=' * 60)
                    logger.info('[Safety] Sending SIGINT to main process to trigger relay shutdowns...')
                    os.kill(os.getpid(), signal.SIGINT)
                    time.sleep(5.0)
                    logger.critical('[Safety] Pytest failed to exit cleanly within 5 seconds. Executing hard kill.')
                    os._exit(1)
            except Exception as e:
                consecutive_errors += 1
                logger.warning('transient_gpio_read_error_consecutive_errors_max_consecutive_errors_e', consecutive_errors=consecutive_errors, MAX_CONSECUTIVE_ERRORS=MAX_CONSECUTIVE_ERRORS, e=e)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.critical('fatal_watchdog_hardware_failure_mid_test_max_consecutive_errors_consecutive_gpio_read_errors', MAX_CONSECUTIVE_ERRORS=MAX_CONSECUTIVE_ERRORS)
                    logger.critical('[Safety] Halting process to prevent unmonitored high-voltage hazards.')
                    os.kill(os.getpid(), signal.SIGINT)
                    return
            time.sleep(self.cfg.polling_interval_s)