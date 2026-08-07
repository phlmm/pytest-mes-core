import structlog
from typing import Protocol, runtime_checkable

logger = structlog.get_logger('mes_core.transports.mcu')

@runtime_checkable
class McuTransport(Protocol):
    """
    Contract for Bare-Metal MCU Transports (e.g., SWD/JTAG Debug Probes).
    Any custom PyOCD or OpenOCD driver must implement this interface.
    """
    
    @property
    def is_connected(self) -> bool:
        """Returns True if the debug probe is actively attached to the MCU."""
        ...

    def connect(self) -> None:
        """Attaches the debug probe to the target MCU."""
        ...

    def disconnect(self) -> None:
        """Detaches the debug probe to prevent state leakage."""
        ...

    def halt(self) -> None:
        """Pauses the CPU core (halts the program counter)."""
        ...

    def resume(self) -> None:
        """Resumes execution of the CPU core."""
        ...

    def reset(self) -> None:
        """Triggers a physical or vector-catch reset of the MCU."""
        ...

    def read_memory(self, address: int, size: int) -> bytes:
        """Reads a block of memory from the MCU SRAM/Flash."""
        ...

    def write_memory(self, address: int, data: bytes) -> None:
        """Writes a block of memory to the MCU SRAM/Flash."""
        ...

    def read_core_register(self, reg_name: str) -> int:
        """Reads an internal CPU register (e.g., 'PC', 'SP', 'LR')."""
        ...








