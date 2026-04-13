# src/pytest_mes_core/provisioning/__init__.py

"""
MES Core Provisioning Layer
---------------------------
Manages the physical and cryptographic injection of firmware,
operating systems, and security credentials onto bare silicon.
Enforces strict validation, host-safety, and hardware isolation.
"""

# Contracts & Exceptions
from .base import (
    BaseProvisioner,
    ProvisioningError,
    ImageVerificationError,
    SiliconLockError
)

# Bootstrapping & Hardware State
from .bootstrap import HardwareBootstrapper

# Network Asset Fetching
from .secure_fetch import SecureAssetFetcher, SecureFetchError

# Flashing Tools
from .jtag import OpenOcdRpcProvisioner
from .microchip import MicrochipIpeProvisioner
from .tezi_uuu import UuuTeziProvisioner
from .block_device import BmapBlockDeviceProvisioner

# Cryptography
from .pki import PkiProvisioner, PkiPairingValidator

__all__ = [
    # Contracts & Exceptions
    "BaseProvisioner",
    "ProvisioningError",
    "ImageVerificationError",
    "SiliconLockError",
    "SecureFetchError",

    # Bootstrapping
    "HardwareBootstrapper",

    # Network Assets
    "SecureAssetFetcher",

    # Flashing Tools
    "OpenOcdRpcProvisioner",
    "MicrochipIpeProvisioner",
    "UuuTeziProvisioner",
    "BmapBlockDeviceProvisioner",

    # Cryptography
    "PkiProvisioner",
    "PkiPairingValidator"
]
