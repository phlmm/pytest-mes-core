# src/pytest_mes_core/provisioning/base.py
from abc import ABC, abstractmethod
from pathlib import Path
import logging

class ProvisioningError(Exception):
    """Root exception for firmware flashing failures."""
    pass

class ImageVerificationError(ProvisioningError):
    """Raised when the image flashes, but the CRC/Hash verification fails."""
    pass

class SiliconLockError(ProvisioningError):
    """Raised when the chip rejects the flash due to blown security fuses/readback protection."""
    pass

class BaseProvisioner(ABC):
    """
    Abstract Base Class for all factory provisioning tools (JTAG, Fastboot, TEZI, UUU).
    """
    @abstractmethod
    def provision(self, image_path: Path) -> bool:
        """
        Returns True if successful, raises ProvisioningError or returns False on failure.
        """
        pass
