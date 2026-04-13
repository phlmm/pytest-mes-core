# src/pytest_mes_core/host_adapters/microchip.py
import logging
from typing import Any

from pytest_mes_core.host_adapters import BaseHostAdapter, HostAdapterError
from pytest_mes_core.host_adapters import hardware_mutex, HostMutexTimeoutError

logger = logging.getLogger("mes_core.host_adapters.microchip")

class HostPickitAdapter(BaseHostAdapter):
    """
    Manages the physical lock on a Microchip PICkit/ICD probe.
    Ensures parallel Pytest workers do not collide on the same USB interface.
    """
    def __init__(self, tool_serial: str, mutex_timeout_s: float = 60.0):
        self.tool_serial = tool_serial
        self.mutex_timeout_s = mutex_timeout_s
        self._mutex_context = None

    def __enter__(self) -> 'HostPickitAdapter':
        logger.debug(f"[PICkit] Acquiring hardware lock for probe {self.tool_serial}...")

        try:
            # We use the tool's USB serial number as the exact mutex resource name
            self._mutex_context = hardware_mutex(
                resource_name=f"pickit_{self.tool_serial}",
                timeout_s=self.mutex_timeout_s
            )
            self._mutex_context.__enter__()
            return self

        except HostMutexTimeoutError as e:
            raise HostAdapterError(f"Failed to acquire PICkit {self.tool_serial}: {e}")

    def __exit__(self, _exc_type: Any, _exc_val: Any, _exc_tb: Any) -> None:
        """ZERO-LEAKAGE: Release the OS-level hardware lock."""
        if self._mutex_context:
            logger.debug(f"[PICkit] ZERO-LEAKAGE: Releasing lock on {self.tool_serial}.")
            self._mutex_context.__exit__(_exc_type, _exc_val, _exc_tb)
            self._mutex_context = None
