from dataclasses import dataclass
from typing import Any, Protocol
import queue as _queue

# ==========================================
# DOMAIN EXCEPTIONS
# ==========================================
class TransportError(Exception):
    """Base exception for all physical layer transport failures."""
    pass

class TransportConnectionError(TransportError):
    """Raised when the physical pipe (SSH/Serial) shatters or fails to bind."""
    pass

class TransportTimeoutError(TransportError):
    """Raised when the DUT fails to respond within the designated execution window."""
    pass


# ==========================================
# LAYER 1: RAW COMMAND EXECUTION
# ==========================================
@dataclass(frozen=True)
class CommandResult:
    """
    A unified, IMMUTABLE wrapper bridging various transport outputs.
    Guarantees strict data integrity between the physical layer and the parsing protocols.
    """
    command: str
    stdout: str
    stderr: str
    exited: int
    ok: bool
    duration_s: float


# ==========================================
# LAYER 2: TRANSPORT CONTRACT
# ==========================================
class DutTransport(Protocol):
    """
    Structural subtype contract for physical Layer 1 / Layer 3 transports.
    Enforces Command Execution, Lifecycle State Management, and the UART
    pub/sub multiplexer interface.

    All transports (EphemeralSerialClient, EphemeralSSHClient, FailoverTransport)
    must satisfy this contract to be usable as a ``dut_transport`` fixture.
    """

    @property
    def is_connected(self) -> bool:
        """Returns True if the underlying physical socket/descriptor is alive."""
        ...

    def connect(self) -> None:
        """Initializes the hardware interface or network socket."""
        ...

    def disconnect(self) -> None:
        """Safely tears down the interface and flushes buffers (Zero-Leakage)."""
        ...

    def safe_run(
        self,
        cmd: str,
        timeout_s: float = 30.0,
        check_exit_code: bool = False,
        auto_retry: bool = False,
        **kwargs: Any
    ) -> CommandResult:
        """
        Executes a command synchronously on the target.
        Must raise TransportConnectionError if the pipe shatters.
        Must raise TransportTimeoutError if the timeout_s is exceeded.
        """
        ...

    async def async_connect(self) -> None:
        """Async variant of connect."""
        ...

    async def async_disconnect(self) -> None:
        """Async variant of disconnect."""
        ...

    async def async_safe_run(
        self,
        cmd: str,
        timeout_s: float = 30.0,
        check_exit_code: bool = False,
        auto_retry: bool = False,
        **kwargs: Any
    ) -> CommandResult:
        """Async variant of safe_run."""
        ...

    # ==========================================
    # PUB/SUB MULTIPLEXER INTERFACE
    # ==========================================

    def subscribe(self, maxsize: int = 0) -> _queue.Queue:
        """Subscribe to the UART pub/sub multiplexer.

        Returns an independent Queue that receives a copy of every RX byte-chunk
        published by the transport's RX daemon.  Multiple concurrent subscribers
        (watchdog, FSM, external telemetry) all receive every byte independently
        with zero data loss.

        Args:
            maxsize: Queue capacity.  0 = unbounded (recommended for FSM consumers
                     that may be slow relative to the UART baud rate).
        """
        ...

    def unsubscribe(self, q: _queue.Queue) -> None:
        """Remove a previously registered subscriber queue.

        Must be called in a ``finally`` block to guarantee zero-leakage.
        The queue will no longer receive new chunks after this call.
        """
        ...

    def write_line(self, line: str, sensitive: bool = False) -> None:
        """Transmit ``line`` followed by a newline terminator.

        Args:
            line: The text to send (without trailing newline).
            sensitive: If True, the payload must NOT be logged in plaintext
                       (e.g. passwords, tokens).
        """
        ...

    def raw_write(self, data: bytes) -> None:
        """Transmit raw bytes without framing or newline injection."""
        ...

    async def async_raw_write(self, data: bytes) -> None:
        """Async variant of raw_write."""
        ...

    def raw_read_chunk(self) -> bytes:
        """Reads a chunk of raw bytes."""
        ...

    async def async_raw_read_chunk(self) -> bytes:
        """Async variant of raw_read_chunk."""
        ...

    def raw_read(self, size: int) -> bytes:
        """Reads exactly 'size' raw bytes."""
        ...

    async def async_raw_read(self, size: int) -> bytes:
        """Async variant of raw_read."""
        ...

    def flush_buffers(self) -> None:
        """Flush hardware RX/TX buffers and drain all subscriber queues.

        Calling this before opening an event stream ensures the event loop
        does not process stale data from a previous boot cycle.
        """
        ...

    async def async_flush_buffers(self) -> None:
        """Async variant of flush_buffers."""
        ...

    def expect(self, pattern: str, timeout_s: float = 5.0, blast_char: str = '', active_redraw: bool = True) -> str:
        """Wait for a regex pattern to appear on the stream."""
        ...

    async def async_expect(self, pattern: str, timeout_s: float = 5.0, blast_char: str = '', active_redraw: bool = True) -> str:
        """Async variant of expect."""
        ...

