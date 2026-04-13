# src/pytest_mes_core/host_adapters/__init__.py

"""
MES Core Host Adapters Layer
----------------------------
Manages physical hardware attached to the Host PC / Test Jig.
Enforces Zero-Leakage teardown and jig-level fault isolation.
"""

from .base import (
    BaseHostAdapter,
    HostAdapterError,
    HostResourceBusyError,
    HostHardwareDisconnectError
)

from .mutex import hardware_mutex, HostMutexTimeoutError
from .can_bus import HostCanAdapter, HostCanError
from .hid_scanner import HeadlessBarcodeScanner, HidScannerTimeoutError
from .serial_uart import HostSerialAdapter, HostSerialError
from .safety import EStopWatchdog
from .openocd import OpenOcdDaemonAdapter, HostOpenOcdError
from .microchip import HostPickitAdapter

__all__ = [
    # Contracts & Exceptions
    "BaseHostAdapter",
    "HostAdapterError",
    "HostResourceBusyError",
    "HostHardwareDisconnectError",
    "HostMutexTimeoutError",
    "HostCanError",
    "HidScannerTimeoutError",
    "HostSerialError",

    # IPC
    "hardware_mutex",

    # Physical Adapters
    "HostCanAdapter",
    "HeadlessBarcodeScanner",
    "HostSerialAdapter",
    "EStopWatchdog",

    # JTAG
    "OpenOcdDaemonAdapter", "HostOpenOcdError",
    "HostPickitAdapter"
]
