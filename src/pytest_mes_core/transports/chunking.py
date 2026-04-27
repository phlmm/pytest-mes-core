import structlog
import time
import threading
import logging
from typing import List, Optional
from pytest_mes_core.transports.base import DutTransport, TransportConnectionError
logger = structlog.get_logger('mes_core.transports.chunking')

class HostSideBuffer:
    """
    Asynchronous Data Vacuum.
    Continuously tails a remote log file across ANY transport and buffers it in Host PC RAM.
    Guarantees data survival even if the DUT kernel panics and the socket drops.
    """

    def __init__(self, transport: DutTransport, remote_path: str, poll_interval_s: float=1.0):
        self.transport = transport
        self.remote_path = remote_path
        self.poll_interval_s = poll_interval_s           # user-visible; never mutated
        self._effective_poll_s = max(poll_interval_s, 0.5)  # enforced floor
        self._buffer: List[str] = []
        self._lines_read = 0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0: float = 0.0

    def start(self) -> None:
        """Spawns the background daemon to begin data extraction.

        Ensures thread safety and floors the polling interval to protect DUT CPU.
        The user-visible ``poll_interval_s`` attribute is never mutated; the floor
        is applied to the internal ``_effective_poll_s`` only.
        """
        if self._thread and self._thread.is_alive():
            logger.warning('vacuum_for_remote_path_is_already_running_ignoring_start_request', remote_path=self.remote_path)
            return
        if self.poll_interval_s < 0.5:
            logger.warning('poll_interval_poll_interval_s_s_is_too_fast_flooring_to_0_5s_to_protect_dut_cpu', poll_interval_s=self.poll_interval_s)
        self._effective_poll_s = max(self.poll_interval_s, 0.5)
        logger.info('arming_asynchronous_vacuum_for_remote_path', remote_path=self.remote_path)
        self._stop_event.clear()
        self._t0 = time.perf_counter()
        with self._lock:
            self._buffer.clear()
            self._lines_read = 0
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> List[str]:
        """Halts the polling instantly and returns a thread-safe copy of the surviving data.

        Returns:
            List[str]: The extracted log lines secured in Host RAM.
        """
        logger.debug('[HostBuffer] ZERO-LEAKAGE: Disarming vacuum and reaping thread...')
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._effective_poll_s + 0.5)
            if self._thread.is_alive():
                logger.warning('thread_join_timed_out_transport_socket_severely_hung')
        duration = round(time.perf_counter() - self._t0, 2)
        with self._lock:
            survived_data = list(self._buffer)
            logger.info('vacuum_disarmed_extracted_val_lines_over_duration_s', val=len(survived_data), duration=duration)
            return survived_data

    async def async_start(self) -> None:
        """Async variant of start using anyio threads."""
        import anyio
        await anyio.to_thread.run_sync(self.start)

    async def async_stop(self) -> List[str]:
        """Async variant of stop using anyio threads."""
        import anyio
        return await anyio.to_thread.run_sync(self.stop)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    start_line = self._lines_read + 1
                cmd = f'tail -n +{start_line} {self.remote_path} 2>/dev/null'
                is_failed_over = getattr(self.transport, 'is_failed_over', False)
                if is_failed_over:
                    try:
                        fallback = getattr(self.transport, 'fallback', None)
                        if fallback:
                            parser = getattr(fallback, 'parser', None)
                            if parser:
                                new_lines = parser.extract_lines()
                                if new_lines:
                                    with self._lock:
                                        self._buffer.extend(new_lines)
                                        self._lines_read += len(new_lines)
                                    logger.debug('passive_rx_val_lines_from_watchdog_total_lines_read', val=len(new_lines), _lines_read=self._lines_read)
                    except Exception:
                        pass
                    self._stop_event.wait(timeout=self.poll_interval_s)
                    continue
                run_timeout = max(2.0, self.poll_interval_s * 1.5)
                logger.debug('tx_cmd', cmd=cmd)
                res = self.transport.safe_run(cmd, timeout_s=run_timeout)
                if res.ok and res.stdout:
                    new_lines = [l for l in res.stdout.strip().split('\n') if l.strip()]
                    if new_lines:
                        with self._lock:
                            self._buffer.extend(new_lines)
                            self._lines_read += len(new_lines)
                        logger.debug('rx_val_new_lines_extracted_total_lines_read', val=len(new_lines), _lines_read=self._lines_read)
            except TransportConnectionError:
                with self._lock:
                    survived = len(self._buffer)
                logger.warning('transport_severed_dut_crash_panic_hardware_disconnect_detected')
                logger.warning('vacuum_aborting_survived_lines_successfully_secured_in_host_ram', survived=survived)
                break
            except Exception as e:
                with self._lock:
                    survived = len(self._buffer)
                logger.error('vacuum_thread_encountered_an_unexpected_fault_e', e=e)
                logger.error('aborting_survived_lines_successfully_secured_in_host_ram', survived=survived)
                break
            self._stop_event.wait(timeout=self._effective_poll_s)