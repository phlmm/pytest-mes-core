# src/pytest_mes_core/host_adapters/base.py
from abc import ABC, abstractmethod
from typing import Any

# ==========================================
# DOMAIN EXCEPTIONS (Jig Faults)
# ==========================================
class HostAdapterError(Exception):
    """
    Root exception for all Host PC physical adapter failures.
    Catching this guarantees the failure was on the Test Jig, not the DUT.
    """
    pass

class HostResourceBusyError(HostAdapterError):
    """Raised when a resource (COM port, CAN socket, USB HID) is locked by another process."""
    pass

class HostHardwareDisconnectError(HostAdapterError):
    """Raised when an adapter is physically unplugged mid-test (e.g., ENODEV)."""
    pass


# ==========================================
# STRUCTURAL CONTRACT
# ==========================================
class BaseHostAdapter(ABC):
    """
    Abstract Base Class for all Host PC hardware adapters.
    Strictly enforces the Context Manager pattern to mathematically guarantee
    Zero-Leakage teardown across the factory floor.
    """

    @abstractmethod
    def __enter__(self) -> 'BaseHostAdapter':
        """
        Must acquire the hardware lock, perform OS-level pre-flight checks,
        and bind to the physical socket/port.
        """
        pass

    @abstractmethod
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """
        Must cleanly release the hardware resource and flush buffers,
        regardless of whether the test passed, failed, or crashed.
        """
        pass

    async def __aenter__(self) -> 'BaseHostAdapter':
        """Async context manager wrapper. Delegates to __enter__ via thread."""
        import anyio
        return await anyio.to_thread.run_sync(self.__enter__)

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager wrapper. Delegates to __exit__ via thread."""
        import anyio
        await anyio.to_thread.run_sync(self.__exit__, exc_type, exc_val, exc_tb)
