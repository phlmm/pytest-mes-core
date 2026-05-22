import structlog
import time
import logging
from contextlib import contextmanager
from typing import Iterator, Any

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
    fcntl = _DummyFcntl()
from pytest_mes_core.host_adapters.base import HostAdapterError
logger = structlog.get_logger('mes_core.host_adapters.mutex')

class HostMutexTimeoutError(HostAdapterError):
    """Raised when a pytest-xdist worker fails to acquire a shared hardware resource."""
    pass

@contextmanager
def hardware_mutex(resource_name: str, timeout_s: float=60.0) -> Iterator[None]:
    """Prevents parallel pytest-xdist workers from colliding on physical USB hardware.

    Defensively logs waiting states, prevents deadlocks, and relies on the OS kernel
    for bulletproof lock release even on SIGKILL.

    Args:
        resource_name: The unique string identifier for the hardware resource.
        timeout_s: Maximum time to wait for the lock before aborting.

    Yields:
        None

    Raises:
        HostMutexTimeoutError: If the lock cannot be acquired within the timeout.
    """
    if not HAS_FCNTL:
        logger.warning('fcntl_not_available_on_this_os_hardware_lock_resource_name_bypassed_do_not_run_parallel_tests', resource_name=resource_name)
        yield
        return
    lock_file = f'/tmp/mes_hw_{resource_name}.lock'
    logger.debug('attempting_to_acquire_os_lock_on_lock_file', lock_file=lock_file)
    t0 = time.perf_counter()
    waiting_logged = False
    with open(lock_file, 'a') as f:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waiting_logged:
                    logger.info('hardware_resource_name_is_currently_busy_worker_queued_timeout_timeout_s_s', resource_name=resource_name, timeout_s=timeout_s)
                    waiting_logged = True
                if time.perf_counter() - t0 > timeout_s:
                    err_msg = f'Failed to acquire {resource_name} after {timeout_s}s. Deadlock or ghost worker?'
                    logger.critical('fatal_err_msg', err_msg=err_msg)
                    raise HostMutexTimeoutError(err_msg)
                time.sleep(0.2)
        wait_duration = time.perf_counter() - t0
        if waiting_logged:
            logger.info('worker_successfully_acquired_resource_name_after_waiting_wait_duration_s', resource_name=resource_name, wait_duration=wait_duration)
        else:
            logger.debug('acquired_resource_name_instantly', resource_name=resource_name)
        try:
            yield
        finally:
            logger.debug('zero_leakage_releasing_os_lock_on_resource_name', resource_name=resource_name)
            fcntl.flock(f, fcntl.LOCK_UN)

import anyio
from contextlib import asynccontextmanager
from typing import AsyncIterator

@asynccontextmanager
async def async_hardware_mutex(resource_name: str, timeout_s: float=60.0) -> AsyncIterator[None]:
    """Prevents parallel pytest-xdist workers from colliding on physical USB hardware.
    Non-blocking async wrapper.
    """
    if not HAS_FCNTL:
        logger.warning('fcntl_not_available_on_this_os_hardware_lock_resource_name_bypassed_do_not_run_parallel_tests', resource_name=resource_name)
        yield
        return
    lock_file = f'/tmp/mes_hw_{resource_name}.lock'
    logger.debug('attempting_to_acquire_os_lock_on_lock_file', lock_file=lock_file)
    t0 = time.perf_counter()
    waiting_logged = False
    with open(lock_file, 'a') as f:
        while True:
            try:
                # To prevent blocking, we use run_sync for the fcntl call if needed, 
                # or just run it inline since LOCK_NB is non-blocking.
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waiting_logged:
                    logger.info('hardware_resource_name_is_currently_busy_worker_queued_timeout_timeout_s_s', resource_name=resource_name, timeout_s=timeout_s)
                    waiting_logged = True
                if time.perf_counter() - t0 > timeout_s:
                    err_msg = f'Failed to acquire {resource_name} after {timeout_s}s. Deadlock or ghost worker?'
                    logger.critical('fatal_err_msg', err_msg=err_msg)
                    raise HostMutexTimeoutError(err_msg)
                await anyio.sleep(0.2)
        wait_duration = time.perf_counter() - t0
        if waiting_logged:
            logger.info('worker_successfully_acquired_resource_name_after_waiting_wait_duration_s', resource_name=resource_name, wait_duration=wait_duration)
        else:
            logger.debug('acquired_resource_name_instantly', resource_name=resource_name)
        try:
            yield
        finally:
            logger.debug('zero_leakage_releasing_os_lock_on_resource_name', resource_name=resource_name)
            fcntl.flock(f, fcntl.LOCK_UN)