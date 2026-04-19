# src/pytest_mes_core/host_adapters/safety.py
import os
import time
import signal
import threading
import logging
from typing import Optional, Any

# ==========================================
# CROSS-PLATFORM STATIC TYPING STUBS
# ==========================================
class _DummyLine:
    def request(self, *args: Any, **kwargs: Any) -> None: pass
    def get_value(self) -> int: return 1
    def release(self) -> None: pass

class _DummyChip:
    def __init__(self, *args: Any, **kwargs: Any) -> None: pass
    def get_line(self, offset: int) -> _DummyLine: return _DummyLine()
    def close(self) -> None: pass

class _DummyGpiod:
    LINE_REQ_DIR_IN: int = 1
    Chip = _DummyChip

try:
    import gpiod  # type: ignore
    HAS_GPIOD = True
except ImportError:
    HAS_GPIOD = False
    gpiod = _DummyGpiod()  # type: ignore

from pytest_mes_core.config import EStopConfig
from pytest_mes_core.host_adapters.base import BaseHostAdapter, HostAdapterError

logger = logging.getLogger("mes_core.host_adapters.safety")

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
        if not HAS_GPIOD:
            logger.warning("[Safety] gpiod not available. E-Stop bypassed. DANGEROUS IF PHYSICAL HIGH VOLTAGE IS PRESENT!")
            return self

        logger.debug(f"[Safety] Initializing GPIO lock on Host chip{self.cfg.gpiochip}:line{self.cfg.line}...")

        # FAIL-FAST BINDING: We bind in the main thread. If hardware is missing,
        # the test crashes immediately before applying power.
        try:
            self.chip = gpiod.Chip(f"gpiochip{self.cfg.gpiochip}")
            self.line = self.chip.get_line(self.cfg.line)
            self.line.request(consumer="mes_estop", type=gpiod.LINE_REQ_DIR_IN)
        except Exception as e:
            err_msg = f"Failed to bind E-Stop hardware on chip{self.cfg.gpiochip}:line{self.cfg.line}. Error: {e}"
            logger.critical(f"[Safety] FATAL: {err_msg}")
            raise HostAdapterError(err_msg)

        logger.info("[Safety] Watchdog Armed. Physical E-Stop button actively monitored.")

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

        return self

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Disarm the watchdog and release GPIO pins."""
        logger.debug("[Safety] ZERO-LEAKAGE: Disarming E-Stop Watchdog.")
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        # Release kernel GPIO locks
        if self.line:
            try:
                self.line.release()
            except Exception as e:
                logger.debug(f"[Safety] Failed to release line lock: {e}")
        if self.chip:
            try:
                self.chip.close()
            except Exception as e:
                logger.debug(f"[Safety] Failed to close chip: {e}")

    def _monitor(self) -> None:
        """Background thread logic for monitoring physical state."""
        if not self.line:
            return

        trigger_state = 0 if self.cfg.active_low else 1
        logger.debug(f"[Safety] Background poller started. Target trigger state: {trigger_state}")

        # Transient GPIO read failures (EMI, I2C glitch) should not
        # immediately halt the production line. Require consecutive failures.
        MAX_CONSECUTIVE_ERRORS = 3
        consecutive_errors = 0

        while not self._stop_event.is_set():
            try:
                state = self.line.get_value()

                # A successful read resets the error counter
                consecutive_errors = 0

                # Matrix Tracing: Logs the raw boolean logic of the pin
                # This will flood the -vv trace, but is critical for identifying ground loops.
                logger.debug(f"[Safety] Pin State: {state}")

                if state == trigger_state:
                    logger.critical("="*60)
                    logger.critical("FATAL: PHYSICAL E-STOP DEPLOYED! INITIATING EMERGENCY HALT!")
                    logger.critical("="*60)

                    # 1. Send SIGINT to the main thread. Pytest catches this as KeyboardInterrupt
                    # and triggers all context manager __exit__ and fixture teardown blocks.
                    logger.info("[Safety] Sending SIGINT to main process to trigger relay shutdowns...")
                    os.kill(os.getpid(), signal.SIGINT)

                    # 2. Defend against deadlocked main threads (e.g. frozen C-extensions).
                    # Give Pytest 5 seconds to run the high-voltage teardowns...
                    time.sleep(5.0)

                    # 3. If the process is still alive after 5s, execute the violent kill.
                    logger.critical("[Safety] Pytest failed to exit cleanly within 5 seconds. Executing hard kill.")
                    os._exit(1)

            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"[Safety] Transient GPIO read error ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")

                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.critical(f"[Safety] FATAL: Watchdog hardware failure mid-test! {MAX_CONSECUTIVE_ERRORS} consecutive GPIO read errors.")
                    logger.critical("[Safety] Halting process to prevent unmonitored high-voltage hazards.")
                    # If safety monitoring physically fails (e.g., operator unplugs GPIO wire),
                    # we MUST halt the line to prevent unmonitored hazards.
                    os.kill(os.getpid(), signal.SIGINT)
                    return

            time.sleep(self.cfg.polling_interval_s)

