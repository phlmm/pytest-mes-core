import logging
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from abc import ABC, abstractmethod

logger = logging.getLogger("mes_core.manifest")

@dataclass
class ComponentIdentity:
    """Standardized identity block for any active silicon on the board."""
    name: str
    serial_number: Optional[str] = None
    hardware_rev: Optional[str] = None
    firmware_rev: Optional[str] = None

    @property
    def is_identified(self) -> bool:
        return self.serial_number is not None

class HardwareManifest(ABC):
    """
    Abstract Base Class for hardware genealogy.
    Test projects must inherit from this to define their specific board schema.
    """
    def to_dict(self) -> Dict[str, Any]:
        """Generic serialization that strips null values to save JSONL space."""
        raw_dict = asdict(self)

        def remove_nulls(d):
            return {k: remove_nulls(v) if isinstance(v, dict) else v
                    for k, v in d.items() if v is not None}

        return remove_nulls(raw_dict)

    @abstractmethod
    def check_completeness(self) -> list[str]:
        """Projects must implement this to verify their required components."""
        pass
