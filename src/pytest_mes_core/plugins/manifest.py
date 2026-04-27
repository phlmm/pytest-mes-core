import structlog
import pytest
import logging
import threading
from typing import Optional, Tuple, Type
from pytest_mes_core.manifest import HardwareManifest, ManifestScraper
from pytest_mes_core.telemetry import TelemetryExporter
logger = structlog.get_logger('mes_core.plugins.manifest')
_manifest_lock = threading.Lock()
_cached_manifest: Optional[HardwareManifest] = None

@pytest.fixture(scope='session')
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

@pytest.fixture(scope='function')
def dut_manifest(dut_transport, dut_state_machine, dut_manifest_scraper: Tuple[Type[ManifestScraper], Type[HardwareManifest]], telemetry_sink: Optional[TelemetryExporter]) -> HardwareManifest:
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
    if _cached_manifest:
        return _cached_manifest
    with _manifest_lock:
        if _cached_manifest:
            return _cached_manifest
        if not dut_transport.is_connected:
            logger.warning('[Manifest] Cannot scrape manifest: DUT is not physically connected or booted.')
            scraper_cls, manifest_cls = dut_manifest_scraper
            return manifest_cls()
        scraper_cls, _ = dut_manifest_scraper
        scraper = scraper_cls(dut_transport)
        _cached_manifest = scraper.harvest()
        if telemetry_sink and telemetry_sink.context:
            telemetry_sink.context.dut_serial = _cached_manifest.serial_number
            hw_dict = _cached_manifest.model_dump()
            # Strip software_manifest — it lives in its own section of the report.
            # Leaving it in dut_manifest would cause it to render inside the
            # Hardware Manifest (Station BOM) table.
            hw_dict.pop('software_manifest', None)
            telemetry_sink.context.dut_manifest = hw_dict
            telemetry_sink.context.software_manifest = getattr(_cached_manifest, 'software_manifest', {})
            logger.info('live_telemetry_stream_updated_for_sn_serial_number', serial_number=_cached_manifest.serial_number)
        return _cached_manifest

@pytest.fixture(scope='session', autouse=True)
def _clear_manifest_cache() -> None:
    """Ensures the cache resets if multiple Jigs are run sequentially."""
    global _cached_manifest
    with _manifest_lock:
        _cached_manifest = None