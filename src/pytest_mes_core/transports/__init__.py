"""
MES Core Transport Layer
------------------------
Manages all physical Layer 1 / Layer 3 communications with the Device Under Test.
Provides a strict, transport-agnostic contract for downstream physics protocols.
"""

# 1. The Core Contracts & Exceptions
from .base import (
    DutTransport,
    CommandResult,
    TransportError,
    TransportConnectionError,
    TransportTimeoutError
)

# 2. The Physical Implementations
from .ssh import EphemeralSSHClient
from .serial_client import EphemeralSerialClient
from .mqtt_client import MqttClient
from .pyocd_client import PyOcdTransport
from .probe_rs_client import ProbeRsTransport, ProbeRsError

# 3. The Auto-Healing Matrix
from .failover import FailoverTransport

# 4. Utilities
from .chunking import HostSideBuffer

# ==========================================
# STRICT PUBLIC API BOUNDARY
# ==========================================
# Only the classes listed here will be exported when a user types:
# `from pytest_mes_core.transports import *`
__all__ = [
    # Contracts
    "DutTransport",
    "CommandResult",

    # Exceptions
    "TransportError",
    "TransportConnectionError",
    "TransportTimeoutError",

    # Clients
    "EphemeralSSHClient",
    "EphemeralSerialClient",
    "MqttClient",
    "PyOcdTransport",
    "ProbeRsTransport",
    "ProbeRsError",
    "FailoverTransport",

    # Utilities
    "HostSideBuffer"
]
