# src/pytest_mes_core/plugins/manifest.py
import pytest
import logging
import threading
from typing import Optional, Tuple, Type

from pytest_mes_core.manifest import HardwareManifest, ManifestScraper
from pytest_mes_core.telemetry import TelemetryExporter

logger = logging.getLogger("mes_core.plugins.manifest")

# Thread-safe cache to prevent scraping the board 50 times during a 50-test suite.
# Uses a lock to prevent race conditions under thread-based pytest-xdist workers.
_manifest_lock = threading.Lock()
_cached_manifest: Optional[HardwareManifest] = None

@pytest.fixture(scope="session")
def dut_manifest_scraper() -> Tuple[Type[ManifestScraper], Type[HardwareManifest]]:
    """
    Declares which scraper + manifest class pair to use for hardware genealogy.

    Override this fixture in your project's conftest.py to inject a
    project-specific scraper subclass (e.g., EvseManifestScraper):

        @pytest.fixture(scope="session")
        def dut_manifest_scraper():
            from projects.evse_board.manifest import EvseManifestScraper, EvseHardwareManifest
            return (EvseManifestScraper, EvseHardwareManifest)

    Returns:
        Tuple[Type[ManifestScraper], Type[HardwareManifest]]: The scraper and
        manifest class to use for this test session.
    """
    return (ManifestScraper, HardwareManifest)

@pytest.fixture(scope="function")
def dut_manifest(
    dut_transport,
    dut_state_machine,  # We depend on FSM to ensure OS is booted
    dut_manifest_scraper: Tuple[Type[ManifestScraper], Type[HardwareManifest]],
    telemetry_sink: Optional[TelemetryExporter]
) -> HardwareManifest:
    """
    Provides the indisputable hardware manifest of the active DUT.
    Automatically caches the result per-session to save boot time, and securely
    injects the Serial Number and BOM directly into the Telemetry stream.

    The scraper class used is determined by the ``dut_manifest_scraper`` fixture,
    which projects can override to supply a subclass with additional probing logic.

    Thread-safe: uses a lock to prevent duplicate scrapes under pytest-xdist
    thread-based parallelism.
    """
    global _cached_manifest

    # Fast path: return cached manifest without lock contention
    if _cached_manifest:
        return _cached_manifest

    with _manifest_lock:
        # Double-checked locking: another thread may have populated the cache
        # while we were waiting for the lock.
        if _cached_manifest:
            return _cached_manifest

        # 1. If the board isn't booted to Linux, we can't scrape it.
        if not dut_transport.is_connected:
            logger.warning("[Manifest] Cannot scrape manifest: DUT is not physically connected or booted.")
            scraper_cls, manifest_cls = dut_manifest_scraper
            return manifest_cls()

        # 2. Perform the physical scrape using the (possibly overridden) scraper class
        scraper_cls, _ = dut_manifest_scraper
        scraper = scraper_cls(dut_transport)
        _cached_manifest = scraper.harvest()

        # 3. TELEMETRY INJECTION (Zero-Leakage Integration)
        # The moment we learn the Serial Number, we stamp it onto the master telemetry session
        if telemetry_sink and telemetry_sink.context:
            telemetry_sink.context.dut_serial = _cached_manifest.serial_number
            telemetry_sink.context.dut_manifest = _cached_manifest.model_dump()
            telemetry_sink.context.software_manifest = getattr(_cached_manifest, "software_manifest", {})
            logger.info(f"[Manifest] Live telemetry stream updated for SN-{_cached_manifest.serial_number}")

        return _cached_manifest

@pytest.fixture(scope="session", autouse=True)
def _clear_manifest_cache() -> None:
    """Ensures the cache resets if multiple Jigs are run sequentially."""
    global _cached_manifest
    with _manifest_lock:
        _cached_manifest = None

