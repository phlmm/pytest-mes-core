import structlog
from typing import Any
from contextlib import contextmanager
from pytest_mes_core.host_adapters import BaseHostAdapter, HostAdapterError
from pytest_mes_core.host_adapters import hardware_mutex, HostMutexTimeoutError
logger = structlog.get_logger('mes_core.host_adapters.microchip')

class HostPickitAdapter(BaseHostAdapter):
    """
    Manages the physical lock on a Microchip PICkit/ICD probe.
    Ensures parallel Pytest workers do not collide on the same USB interface.
    """

    def __init__(self, tool_serial: str, mutex_timeout_s: float=60.0):
        self.tool_serial = tool_serial
        self.mutex_timeout_s = mutex_timeout_s
        self._mutex_context = None

    def __enter__(self) -> 'HostPickitAdapter':
        """Acquires a cross-process mutex lock for the physical PICkit probe.

        Returns:
            HostPickitAdapter: The locked hardware adapter instance.

        Raises:
            HostAdapterError: If the probe cannot be locked within the timeout.
        """
        logger.debug('acquiring_hardware_lock_for_probe_tool_serial', tool_serial=self.tool_serial)
        try:
            self._mutex_context = hardware_mutex(resource_name=f'pickit_{self.tool_serial}', timeout_s=self.mutex_timeout_s)
            self._mutex_context.__enter__()
            logger.info('hardware_lock_successfully_acquired_for_probe_tool_serial', tool_serial=self.tool_serial)
            return self
        except HostMutexTimeoutError as e:
            err_msg = f'Failed to acquire PICkit {self.tool_serial}: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise HostAdapterError(err_msg)

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Release the OS-level hardware lock."""
        if self._mutex_context:
            logger.debug('zero_leakage_releasing_lock_on_tool_serial', tool_serial=self.tool_serial)
            self._mutex_context.__exit__(exc_type, exc_val, exc_tb)
            self._mutex_context = None

    @contextmanager
    def lock_usb_bus(self):
        """Allows tests to use 'with pickit.lock_usb_bus():' for better readability,
        while routing through the required __enter__/__exit__ methods.

        Yields:
            HostPickitAdapter: The locked hardware adapter instance.
        """
        with self:
            yield self

    def async_lock_usb_bus(self):
        """Async version of lock_usb_bus()."""
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _async_lock():
            async with self:
                yield self
        return _async_lock()
