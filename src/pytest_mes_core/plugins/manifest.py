# src/pytest_mes_core/plugins/manifest.py
import pytest
import logging
from typing import Optional

from pytest_mes_core.manifest import HardwareManifest, ManifestScraper
from pytest_mes_core.telemetry import TelemetryExporter

logger = logging.getLogger("mes_core.plugins.manifest")

# Cache to prevent scraping the board 50 times during a 50-test suite
_cached_manifest: Optional[HardwareManifest] = None

@pytest.fixture(scope="function")
def dut_manifest(
    dut_transport,
    dut_state_machine, # We depend on FSM to ensure OS is booted
    telemetry_sink: Optional[TelemetryExporter]
) -> HardwareManifest:
    """
    Provides the indisputable hardware manifest of the active DUT.
    Automatically caches the result per-session to save boot time, and securely
    injects the Serial Number and BOM directly into the Telemetry stream.
    """
    global _cached_manifest

    # 1. Return the cache if we already scraped this board
    if _cached_manifest:
        return _cached_manifest

    # 2. If the board isn't booted to Linux, we can't scrape it.
    if not dut_transport.is_connected:
        logger.warning("[Manifest] Cannot scrape manifest: DUT is not physically connected or booted.")
        return HardwareManifest()

    # 3. Perform the physical scrape
    scraper = ManifestScraper(dut_transport)
    _cached_manifest = scraper.harvest()

    # 4. 🚨 TELEMETRY INJECTION (Zero-Leakage Integration)
    # The moment we learn the Serial Number, we stamp it onto the master telemetry session
    if telemetry_sink and telemetry_sink.context:
        telemetry_sink.context.dut_serial = _cached_manifest.serial_number
        telemetry_sink.context.dut_manifest = _cached_manifest.model_dump()
        logger.info(f"[Manifest] Live telemetry stream updated for SN-{_cached_manifest.serial_number}")

    return _cached_manifest

@pytest.fixture(scope="session", autouse=True)
def _clear_manifest_cache() -> None:
    """Ensures the cache resets if multiple Jigs are run sequentially."""
    global _cached_manifest
    _cached_manifest = None
