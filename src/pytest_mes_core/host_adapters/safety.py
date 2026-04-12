# src/pytest_mes_core/host_adapters/safety.py
import os
import time
import signal
import threading
import logging
import gpiod  # type: ignore
from typing import Optional

from pytest_mes_core.config import EStopConfig

logger = logging.getLogger("mes_core.host_adapters.safety")

class EStopWatchdog:
    """
    Runs completely outside the pytest event loop.
    Monitors a physical hardware E-Stop button.
    Triggers a graceful but immediate cascade failure to ensure high-voltage teardowns execute.
    """
    def __init__(self, cfg: EStopConfig):
        self.cfg = cfg
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        logger.info(f"[Safety] Arming E-Stop Watchdog on Host chip{self.cfg.gpiochip}:line{self.cfg.line}...")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def _monitor(self) -> None:
        try:
            # Bind to the physical Host PC hardware
            chip = gpiod.Chip(f"gpiochip{self.cfg.gpiochip}")
            line = chip.get_line(self.cfg.line)
            line.request(consumer="mes_estop", type=gpiod.LINE_REQ_DIR_IN)

            trigger_state = 0 if self.cfg.active_low else 1

            while not self._stop_event.is_set():
                state = line.get_value()

                if state == trigger_state:
                    logger.critical("="*60)
                    logger.critical("FATAL: PHYSICAL E-STOP DEPLOYED! INITIATING EMERGENCY HALT!")
                    logger.critical("="*60)

                    # 1. Send SIGINT to the main thread. Pytest catches this and runs teardowns.
                    os.kill(os.getpid(), signal.SIGINT)

                    # 2. Defend against deadlocked main threads.
                    # Give Pytest 5 seconds to run the high-voltage teardowns...
                    time.sleep(5.0)

                    # 3. If the process is still alive after 5s, execute the violent kill.
                    logger.critical("[Safety] Pytest failed to exit cleanly. Executing hard kill.")
                    os._exit(1)

                time.sleep(self.cfg.polling_interval_s)

        except Exception as e:
            logger.error(f"[Safety] Watchdog hardware failure! {e}")
            # If safety monitoring fails, we must halt the line.
            os.kill(os.getpid(), signal.SIGINT)
        finally:
            try:
                line.release()
                chip.close()
            except Exception:
                pass

    def stop(self) -> None:
        """Disarms the watchdog at the end of a successful test batch."""
        if self._thread and self._thread.is_alive():
            logger.debug("[Safety] Disarming E-Stop Watchdog.")
            self._stop_event.set()
            self._thread.join(timeout=2.0)
