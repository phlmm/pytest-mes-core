import re
import socket
import threading
import structlog
import collections
import queue
from typing import Optional, Deque
from pytest_mes_core.config.protocols import UdpDiagnosticConfig
from pytest_mes_core.transports.base import TransportTimeoutError

logger = structlog.get_logger('mes_core.transports.udp_logger')

_MAX_LOGS = 50_000

class AsyncUdpLogReceiver:
    def __init__(self, config: UdpDiagnosticConfig):
        self.config = config
        self._logs: Deque[str] = collections.deque(maxlen=_MAX_LOGS)
        self._subscribers = []
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._sock: Optional[socket.socket] = None

    def _listen_loop(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.config.bind_address if self.config.bind_address else "0.0.0.0", self.config.bind_port))
            self._sock.settimeout(0.5)
            logger.info("UDP Log Receiver bound", port=self.config.bind_port)
            
            while not self._stop_event.is_set():
                try:
                    packet, addr = self._sock.recvfrom(4096)
                    decoded = packet.decode('utf-8', errors='ignore').strip()
                    if decoded:
                        self._logs.append(decoded)
                        # Broadcast to all active subscribers
                        for send_stream in self._subscribers.copy():
                            try:
                                send_stream.send_nowait(decoded)
                            except Exception:
                                if send_stream in self._subscribers:
                                    self._subscribers.remove(send_stream)
                except socket.timeout:
                    pass
                except Exception as e:
                    if not self._stop_event.is_set():
                        logger.warning("Error decoding UDP packet", error=e)
        except Exception as e:
            if not self._stop_event.is_set():
                logger.error("UDP socket error", error=e)
        finally:
            if self._sock:
                self._sock.close()
                self._sock = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        for send_stream in self._subscribers:
            send_stream.close()

    def wait_for_regex(self, pattern: str, timeout_s: float=5.0) -> str:
        import time
        regex = re.compile(pattern)
        q = queue.Queue()
        
        class _QueueStream:
            def send_nowait(self, item):
                q.put_nowait(item)
            def close(self):
                pass
                
        stream = _QueueStream()
        self._subscribers.append(stream)
        try:
            for log in self._logs:
                if regex.search(log):
                    return log
            t_end = time.perf_counter() + timeout_s
            while True:
                time_left = t_end - time.perf_counter()
                if time_left <= 0:
                    raise TransportTimeoutError(f'Timed out after {timeout_s}s waiting for UDP log matching: {pattern}')
                try:
                    log = q.get(timeout=time_left)
                    if regex.search(log):
                        return log
                except queue.Empty:
                    raise TransportTimeoutError(f'Timed out after {timeout_s}s waiting for UDP log matching: {pattern}')
        finally:
            if stream in self._subscribers:
                self._subscribers.remove(stream)
