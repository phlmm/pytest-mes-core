# src/pytest_mes_core/transports/base.py
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

    command: str        # The exact payload transmitted to the DUT
    stdout: str
    stderr: str
    exited: int
    ok: bool
    duration_s: float   # High-precision execution time tracked by the transport layer


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

    def safe_run(self, cmd: str, timeout_s: float = 30.0, **kwargs: Any) -> CommandResult:
        """
        Executes a command synchronously on the target.
        Must raise TransportConnectionError if the pipe shatters.
        Must raise TransportTimeoutError if the timeout_s is exceeded.
        """
        ...
