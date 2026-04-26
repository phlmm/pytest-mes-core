import structlog
import re
import anyio
import anyio.from_thread
import logging
from typing import Any, Callable, Optional, Pattern

from pytest_mes_core.events import bus, PanicDetected

logger = structlog.get_logger('mes_core.transports.watchdog')

class UartKernelWatchdog:
    """
    Background asyncio task that monitors the serial stream for kernel panics.
    Runs only when the UART is not actively locked by an expect() call.
    Uses AnyIO for async concurrency and Pluggy for decoupled event dispatch.
    """
    PANIC_PATTERN: Pattern[bytes] = re.compile(b'(Kernel panic - not syncing|Unable to handle kernel paging request|Oops - undefined instruction|Out of memory: Killed process|BUG: soft lockup - CPU|rcu_preempt detected stalls|task blocked for more than 120 seconds|synchronous external abort|mmc\\d+: error -110|EXT4-fs error|UBIFS error|HAB Events|SEC_ERR|Signature Verification Failed)')

    def __init__(self, serial_client: Any):
        self.serial_client = serial_client
        # anyio Events must be created inside an event loop. We'll use a thread-safe flag instead.
        self._panic_event_set = False
        self._cancel_scope = None
        self._panic_msg = ''
        self._rolling_window = b''
        self._thread = None
        self._stop_event = None

    def start(self) -> None:
        """Spawns the background watchdog task to monitor the serial stream."""
        if self._thread and self._thread.is_alive():
            return
            
        import threading
        self._stop_event = threading.Event()
        self._panic_event_set = False
        self._panic_msg = ''
        
        self._thread = threading.Thread(target=self._run_async_in_thread, daemon=True)
        self._thread.start()
        logger.debug('[Watchdog] Kernel panic background watchdog started.')

    def _run_async_in_thread(self):
        anyio.run(self._monitor_loop)

    def stop(self) -> None:
        """Safely stops the watchdog task."""
        if self._stop_event:
            self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        logger.debug('[Watchdog] Kernel panic background watchdog stopped.')

    async def async_start(self) -> None:
        """Async variant of start."""
        import anyio
        await anyio.to_thread.run_sync(self.start)

    async def async_stop(self) -> None:
        """Async variant of stop."""
        import anyio
        await anyio.to_thread.run_sync(self.stop)

    def is_panicked(self) -> bool:
        return self._panic_event_set

    def get_panic_message(self) -> str:
        return self._panic_msg

    async def _monitor_loop(self) -> None:
        import queue
        while not self._stop_event.is_set():
            if not self.serial_client.is_connected:
                await anyio.sleep(0.5)
                continue
                
            q = self.serial_client.subscribe(maxsize=0)
            try:
                while not self._stop_event.is_set() and self.serial_client.is_connected:
                    try:
                        chunk = await anyio.to_thread.run_sync(q.get, True, 0.1)
                        clean_chunk = self.serial_client.ANSI_ESCAPE_B.sub(b'', chunk)
                        self._rolling_window += clean_chunk
                        if len(self._rolling_window) > 1024:
                            self._rolling_window = self._rolling_window[-1024:]
                        if self.PANIC_PATTERN.search(self._rolling_window):
                            logger.critical('=' * 60)
                            logger.critical('[Watchdog] FATAL: ASYNC KERNEL PANIC DETECTED ON UART!')
                            logger.critical('=' * 60)
                            self._panic_msg = 'Async Kernel Panic detected during idle/background monitoring.'
                            self._panic_event_set = True
                            
                            # Dispatch Pydantic Event via Pluggy EventBus
                            import time
                            bus.emit_uart_event(PanicDetected(elapsed_s=time.time(), raw_output=self._panic_msg))
                            break
                    except queue.Empty:
                        await anyio.sleep(0.05)
                    except Exception as e:
                        logger.error(f"Unexpected error in watchdog monitor loop: {e}")
                        await anyio.sleep(0.5)
            finally:
                self.serial_client.unsubscribe(q)
            
            await anyio.sleep(0.5)