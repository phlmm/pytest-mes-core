# src/pytest_mes_core/host_adapters/mutex.py
import time
import fcntl
import logging
from contextlib import contextmanager

logger = logging.getLogger("mes_core.host_adapters.mutex")

@contextmanager
def hardware_mutex(resource_name: str, timeout_s: float = 60.0):
    """
    Prevents parallel pytest-xdist workers from colliding on physical USB hardware.
    Defensively logs waiting states and enforces timeouts to prevent infinite deadlocks.
    """
    lock_file = f"/tmp/mes_hw_{resource_name}.lock"

    logger.debug(f"[Mutex] Attempting to acquire hardware lock: {resource_name}")
    t0 = time.perf_counter()

    with open(lock_file, "w") as f:
        while True:
            try:
                # LOCK_EX | LOCK_NB attempts to grab the lock without blocking infinitely
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if (time.perf_counter() - t0) > timeout_s:
                    logger.critical(f"[Mutex] FATAL: Deadlock! Failed to acquire {resource_name} after {timeout_s}s.")
                    raise TimeoutError(f"Hardware lock {resource_name} timed out.")
                time.sleep(0.5)

        logger.debug(f"[Mutex] Acquired {resource_name}.")
        try:
            yield
        finally:
            logger.debug(f"[Mutex] Releasing {resource_name}.")
            fcntl.flock(f, fcntl.LOCK_UN)
