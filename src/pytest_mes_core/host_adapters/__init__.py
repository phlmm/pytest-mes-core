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
from .host_can_bus import HostCanAdapter
from .hid_scanner import HeadlessBarcodeScanner, HidScannerTimeoutError
from .serial_uart import HostSerialAdapter, HostSerialError
from .peripheral_serial import HostPeripheralSerialAdapter
from .safety import EStopWatchdog
from .openocd import OpenOcdDaemonAdapter, HostOpenOcdError
from .microchip import HostPickitAdapter
from .diagnostics import ResourceDiagnostics

__all__ = [
    # Contracts & Exceptions
    "BaseHostAdapter",
    "HostAdapterError",
    "HostResourceBusyError",
    "HostHardwareDisconnectError",
    "HostMutexTimeoutError",
    "HidScannerTimeoutError",
    "HostSerialError",

    # IPC
    "hardware_mutex",

    # Physical Adapters
    "HostCanAdapter",
    "HeadlessBarcodeScanner",
    "HostSerialAdapter",
    "HostPeripheralSerialAdapter",
    "EStopWatchdog",

    # JTAG
    "OpenOcdDaemonAdapter", "HostOpenOcdError",
    "HostPickitAdapter",

    # Diagnostics
    "ResourceDiagnostics"
]
