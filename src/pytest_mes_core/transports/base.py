from dataclasses import dataclass
from typing import Any, Protocol

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
    Enforces Command Execution AND Lifecycle State Management.
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
