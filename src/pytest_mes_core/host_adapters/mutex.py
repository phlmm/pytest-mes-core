# src/pytest_mes_core/host_adapters/mutex.py
import time
import logging
from contextlib import contextmanager
from typing import Iterator, Any

# ==========================================
# CROSS-PLATFORM STATIC TYPING STUBS
# ==========================================
# fcntl is Unix-only. We use dummy stubs to satisfy strict Pylance/MyPy
# type checkers when developing/linting on Windows or macOS.
class _DummyFcntl:
    LOCK_EX: int = 2
    LOCK_NB: int = 4
    LOCK_UN: int = 8

    @staticmethod
    def flock(fd: Any, operation: int) -> None:
        pass

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False
    fcntl = _DummyFcntl()  # type: ignore


# Import the unified domain exception, DO NOT redefine it locally!
from pytest_mes_core.host_adapters.base import HostAdapterError

logger = logging.getLogger("mes_core.host_adapters.mutex")

class HostMutexTimeoutError(HostAdapterError):
    """Raised when a pytest-xdist worker fails to acquire a shared hardware resource."""
    pass

@contextmanager
def hardware_mutex(resource_name: str, timeout_s: float = 60.0) -> Iterator[None]:
    """
    Prevents parallel pytest-xdist workers from colliding on physical USB hardware.
    Defensively logs waiting states, prevents deadlocks, and relies on the OS kernel
    for bulletproof lock release even on SIGKILL.
    """
    if not HAS_FCNTL:
        logger.warning(f"[Mutex] fcntl not available on this OS. Hardware lock '{resource_name}' bypassed. Do not run parallel tests!")
        yield
        return

    lock_file = f"/tmp/mes_hw_{resource_name}.lock"
    logger.debug(f"[Mutex] Attempting to acquire hardware lock: {resource_name}")
    t0 = time.perf_counter()

    # Use 'a' (append) so we don't truncate the file while another process holds the lock
    with open(lock_file, "a") as f:
        while True:
            try:
                # LOCK_EX (Exclusive) | LOCK_NB (Non-Blocking)
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if (time.perf_counter() - t0) > timeout_s:
                    logger.critical(f"[Mutex] FATAL DEADLOCK: Failed to acquire {resource_name} after {timeout_s}s.")
                    raise HostMutexTimeoutError(
                        f"Resource '{resource_name}' is locked by another process and did not free in time."
                    )
                # Sleep briefly to prevent 100% CPU core pinning while waiting
                time.sleep(0.5)

        logger.debug(f"[Mutex] Acquired {resource_name}.")

        try:
            yield
        finally:
            logger.debug(f"[Mutex] Releasing {resource_name}.")
            # The OS also guarantees release when the file descriptor closes (exiting the `with` block),
            # but explicitly calling LOCK_UN is the cleanest architectural pattern.
            fcntl.flock(f, fcntl.LOCK_UN)
