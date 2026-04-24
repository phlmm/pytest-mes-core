# src/pytest_mes_core/manifest.py
import logging
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
from pytest_mes_core.transports.base import DutTransport

logger = logging.getLogger("mes_core.manifest")

class HardwareManifest(BaseModel):
    """
    The Indisputable Silicon Truth.
    Represents the physical composition of the board currently in the jig.
    """
    serial_number: str = "UNKNOWN"
    board_model: str = "UNKNOWN"
    soc_family: str = "UNKNOWN"
    ram_total_mb: int = 0
    emmc_total_mb: int = 0
    mac_address_eth0: Optional[str] = None
    mac_address_eth1: Optional[str] = None

    # Allows for project-specific custom flags (e.g., "carrier_rev": "C")
    custom_flags: Dict[str, Any] = Field(default_factory=dict)
    
    # Software stack tracking populated by scrapers
    software_manifest: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict:
        """
        Serializes the manifest to a plain dict, stripping null values to save JSONL space.
        Alias for model_dump() provided for backward compatibility with the telemetry layer.
        """
        return {k: v for k, v in self.model_dump().items() if v is not None}

    def check_completeness(self) -> bool:
        """
        Validates that the essential silicon identifiers were successfully harvested.
        This fulfills the Abstract Base Class contract!
        """
        if self.serial_number == "UNKNOWN" or self.board_model == "UNKNOWN":
            logger.warning("[Manifest] Hardware Manifest is incomplete! Essential identities are missing.")
            return False
        return True

class ManifestScraper:
    """
    Universal Linux Hardware Scraper.
    Executes native POSIX commands over the transport to harvest silicon data.
    """
    def __init__(self, transport: DutTransport):
        self.transport = transport

    def harvest(self) -> HardwareManifest:
        """Executes the scraping sequence and strictly validates the output."""
        logger.info("[Manifest] Scraping live hardware genealogy from DUT...")
        manifest = HardwareManifest()

        if not self.transport.is_connected:
            logger.error("[Manifest] Transport severed. Cannot harvest manifest.")
            return manifest

        # 1. Serial Number (Linux Device Tree standard)
        res = self.transport.safe_run("cat /proc/device-tree/serial-number", timeout_s=2.0)
        if res.ok and res.stdout:
            manifest.serial_number = res.stdout.strip().strip('\x00')

        # 2. Board Model
        res = self.transport.safe_run("cat /proc/device-tree/model", timeout_s=2.0)
        if res.ok and res.stdout:
            manifest.board_model = res.stdout.strip().strip('\x00')

        # 3. RAM Capacity (Parsing `free -m`)
        res = self.transport.safe_run("free -m | grep Mem | awk '{print $2}'", timeout_s=2.0)
        if res.ok and res.stdout.isdigit():
            manifest.ram_total_mb = int(res.stdout)

        # 4. Primary eMMC Storage (Assuming mmcblk0)
        res = self.transport.safe_run("cat /sys/block/mmcblk0/size", timeout_s=2.0)
        if res.ok and res.stdout.isdigit():
            # Size is in 512-byte sectors
            manifest.emmc_total_mb = (int(res.stdout) * 512) // (1024 * 1024)

        # 5. MAC Addresses
        res = self.transport.safe_run("cat /sys/class/net/eth0/address", timeout_s=2.0)
        if res.ok and len(res.stdout.strip()) == 17:
            manifest.mac_address_eth0 = res.stdout.strip().upper()

        res = self.transport.safe_run("cat /sys/class/net/eth1/address", timeout_s=2.0)
        if res.ok and len(res.stdout.strip()) == 17:
            manifest.mac_address_eth1 = res.stdout.strip().upper()

        logger.debug(f"[Manifest] Harvest complete: SN-{manifest.serial_number}")

        # Optionally validate the manifest immediately after harvesting
        manifest.check_completeness()

        return manifest
