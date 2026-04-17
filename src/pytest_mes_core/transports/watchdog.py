import re
import time
import logging
import threading
from typing import Any, Callable, Optional, Pattern

logger = logging.getLogger("mes_core.transports.watchdog")

class UartKernelWatchdog:
    """
    Background thread that monitors the serial stream for kernel panics.
    Runs only when the UART is not actively locked by an expect() call.
    """
    
    # Matches common Linux kernel panics and fatal hardware aborts
    PANIC_PATTERN: Pattern[bytes] = re.compile(
        br"(Kernel panic - not syncing|Out of memory: Killed process|synchronous external abort)"
    )

    def __init__(self, serial_client: Any):
        self.serial_client = serial_client
        self._panic_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._panic_msg = ""
        self._panic_callbacks = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._panic_event.clear()
        self._panic_msg = ""
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.debug("[Watchdog] Kernel panic background watchdog started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        logger.debug("[Watchdog] Kernel panic background watchdog stopped.")

    def register_panic_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback to be fired immediately upon panic detection."""
        self._panic_callbacks.append(callback)

    def is_panicked(self) -> bool:
        return self._panic_event.is_set()

    def get_panic_message(self) -> str:
        return self._panic_msg

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self.serial_client.is_connected:
                time.sleep(0.5)
                continue

            # Don't steal bytes if the transport is locked or actively executing an expect/safe_run
            if getattr(self.serial_client, "_is_locked", False) or getattr(self.serial_client, "_is_executing", False):
                time.sleep(0.1)
                continue

            try:
                # Read whatever is in the buffer without blocking
                if self.serial_client.ser and self.serial_client.ser.in_waiting > 0:
                    chunk = self.serial_client.ser.read(self.serial_client.ser.in_waiting)
                    self.serial_client.parser.ingest(chunk)
                    clean_buffer = self.serial_client.ANSI_ESCAPE_B.sub(b'', chunk)
                    
                    if self.PANIC_PATTERN.search(clean_buffer):
                        logger.critical("="*60)
                        logger.critical("[Watchdog] FATAL: ASYNC KERNEL PANIC DETECTED ON UART!")
                        logger.critical("="*60)
                        self._panic_msg = "Async Kernel Panic detected during idle/background monitoring."
                        self._panic_event.set()
                        
                        # Fire callbacks (e.g., to sever SSH sockets and abort blocking calls)
                        for cb in self._panic_callbacks:
                            try:
                                cb()
                            except Exception as e:
                                logger.debug(f"[Watchdog] Panic callback error: {e}")
                                
                        break
            except Exception as e:
                # If serial port fails (e.g. disconnected), sleep a bit
                time.sleep(0.5)

            time.sleep(0.1)
