"""
MES Core Protocols Layer
------------------------
Hardware physics evaluation, telemetry generation, and physical fault isolation.
All protocols strictly return the immutable ValidatorResult contract.
"""

# 1. Core Contracts (The Currency)
from .base import ValidatorResult

# 2. System & Environment
from .environment import EnvironmentValidator
from .time_sync import RtcTimeValidator
from .sysfs_poller import BackgroundSysfsPoller
from .executable import CustomPayloadValidator

# 3. Networking & Communications
from .ethernet import EthernetValidator
from .can_bus import CanBusValidator
from .uart_loopback import UartEchoValidator

# 4. Silicon & Memory
from .memory import NativeMemoryValidator
from .block_storage import BlockDeviceValidator
from .efuse import NvmemEfuseValidator
from .mmio import MmioValidator

# 5. Physical I/O
from .gpio import GpioEdgeValidator, GpioLedActuator, GpioLoopbackValidator
from .iio_data import IioAdcValidator, IioDacActuator

# 6. USB
from .usb import UsbMassStorageValidator


# ==========================================
# STRICT PUBLIC API BOUNDARY
# ==========================================
__all__ = [
    # Contracts
    "ValidatorResult",

    # System & Environment
    "EnvironmentValidator",
    "RtcTimeValidator",
    "BackgroundSysfsPoller",
    "CustomPayloadValidator",

    # Networking & Communications
    "EthernetValidator",
    "CanBusValidator",
    "UartEchoValidator",

    # Silicon & Memory
    "NativeMemoryValidator",
    "BlockDeviceValidator",
    "NvmemEfuseValidator",
    "MmioValidator",

    # Physical I/O
    "GpioEdgeValidator",
    "GpioLedActuator",
    "GpioLoopbackValidator",
    "IioAdcValidator",
    "IioDacActuator",

    # USB
    "UsbMassStorageValidator"
]
