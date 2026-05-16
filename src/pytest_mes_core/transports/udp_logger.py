import re
import anyio
import structlog
from typing import Optional, List
from anyio.abc import TaskGroup, CancelScope
from pytest_mes_core.config.protocols import UdpDiagnosticConfig
from pytest_mes_core.transports.base import TransportTimeoutError

logger = structlog.get_logger('mes_core.transports.udp_logger')

class AsyncUdpLogReceiver:
    def __init__(self, config: UdpDiagnosticConfig):
        self.config = config
        self._logs: List[str] = []
        self._subscribers = []
        self._cancel_scope: Optional[CancelScope] = None

    async def _listen_loop(self):
        try:
            async with await anyio.create_udp_socket(
                local_port=self.config.bind_port,
                local_host=self.config.bind_address if self.config.bind_address else "0.0.0.0",
                reuse_port=True
            ) as sock:
                logger.info("UDP Log Receiver bound", port=self.config.bind_port)
                async for packet, addr in sock:
                    try:
                        decoded = packet.decode('utf-8', errors='ignore').strip()
                        if decoded:
                            self._logs.append(decoded)
                            # Broadcast to all active subscribers
                            for send_stream in self._subscribers.copy():
                                try:
                                    send_stream.send_nowait(decoded)
                                except anyio.WouldBlock:
                                    pass
                                except anyio.ClosedResourceError:
                                    if send_stream in self._subscribers:
                                        self._subscribers.remove(send_stream)
                    except Exception as e:
                        logger.warning("Error decoding UDP packet", error=e)
        except anyio.get_cancelled_exc_class():
            pass
        except Exception as e:
            logger.error("UDP socket error", error=e)

    async def start(self, tg: TaskGroup):
        self._cancel_scope = anyio.CancelScope()
        tg.start_soon(self._run_with_scope)
        
    async def _run_with_scope(self):
        with self._cancel_scope:
            await self._listen_loop()

    async def stop(self):
        if self._cancel_scope:
            self._cancel_scope.cancel()
        for send_stream in self._subscribers:
            send_stream.close()

    async def async_wait_for_regex(self, pattern: str, timeout_s: float = 5.0) -> str:
        regex = re.compile(pattern)
        
        # Check historical logs first
        for log in self._logs:
            if regex.search(log):
                return log
                
        # Register subscriber
        send_stream, receive_stream = anyio.create_memory_object_stream(self.config.buffer_size)
        self._subscribers.append(send_stream)
        
        try:
            with anyio.fail_after(timeout_s):
                async for log in receive_stream:
                    if regex.search(log):
                        return log
        except TimeoutError:
            raise TransportTimeoutError(f"Timed out after {timeout_s}s waiting for UDP log matching: {pattern}")
        finally:
            if send_stream in self._subscribers:
                self._subscribers.remove(send_stream)
            send_stream.close()
            receive_stream.close()
