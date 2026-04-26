import structlog
from typing import Optional
from pytest_mes_core.transports.mcu_base import McuTransport

logger = structlog.get_logger('mes_core.transports.pyocd')

class PyOcdTransport(McuTransport):
    """
    SWD/JTAG Transport implemented using the standard PyOCD library.
    Allows direct memory and register manipulation of ARM Cortex-M MCUs (e.g., STM32).
    """
    def __init__(self, target: str = "stm32h753zitx", frequency: int = 4000000):
        self.target = target
        self.frequency = frequency
        self._session = None
        self._board = None
        self._core = None

    @property
    def is_connected(self) -> bool:
        return self._session is not None and self._session.is_open

    def connect(self) -> None:
        if self.is_connected:
            return
        logger.info("Connecting to SWD Debug Probe...", target=self.target)
        try:
            from pyocd.core.helpers import ConnectHelper
            self._session = ConnectHelper.session_with_chosen_probe(target_override=self.target, frequency=self.frequency)
            self._session.open()
            self._board = self._session.board
            self._core = self._board.target
            logger.info("Successfully connected to MCU core", core_type=self._core.core_type)
        except ImportError:
            raise RuntimeError("pyocd is not installed! Run `pip install pyocd` to use SWD transports.")
        except Exception as e:
            logger.error("Failed to connect via pyOCD", error=str(e))
            raise RuntimeError(f"SWD Connection Failed: {e}")

    def disconnect(self) -> None:
        if self.is_connected:
            logger.debug("Tearing down SWD debug session...")
            self._session.close()
            self._session = None
            self._board = None
            self._core = None

    def halt(self) -> None:
        if self._core:
            logger.debug("Halting MCU core...")
            self._core.halt()

    def resume(self) -> None:
        if self._core:
            logger.debug("Resuming MCU core...")
            self._core.resume()

    def reset(self) -> None:
        if self._core:
            logger.debug("Triggering vector catch reset...")
            self._core.reset()

    def read_memory(self, address: int, size: int) -> bytes:
        """Reads raw bytes directly from the MCU address space."""
        if self._core:
            # pyOCD provides block reads
            data = self._core.read_memory_block8(address, size)
            return bytes(data)
        return b""

    def write_memory(self, address: int, data: bytes) -> None:
        """Writes raw bytes directly to the MCU address space."""
        if self._core:
            self._core.write_memory_block8(address, list(data))

    def read_core_register(self, reg_name: str) -> int:
        """Reads CPU registers (e.g., 'pc', 'sp', 'lr', 'xpsr')."""
        if self._core:
            return self._core.read_core_register(reg_name.lower())
        return 0

    # --- Async Endpoints for Parallel Jig Execution ---

    async def async_connect(self) -> None:
        import anyio
        await anyio.to_thread.run_sync(self.connect)

    async def async_disconnect(self) -> None:
        import anyio
        await anyio.to_thread.run_sync(self.disconnect)

    async def async_halt(self) -> None:
        import anyio
        await anyio.to_thread.run_sync(self.halt)

    async def async_resume(self) -> None:
        import anyio
        await anyio.to_thread.run_sync(self.resume)

    async def async_reset(self) -> None:
        import anyio
        await anyio.to_thread.run_sync(self.reset)
