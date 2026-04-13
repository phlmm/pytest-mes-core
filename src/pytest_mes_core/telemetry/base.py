# src/pytest_mes_core/telemetry/base.py
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, Any, Protocol, Optional

from pytest_mes_core.protocols.base import ValidatorResult

# ==========================================
# DOMAIN EXCEPTIONS
# ==========================================
class TelemetryError(Exception):
    """Base exception for all telemetry routing failures."""
    pass

class TelemetryDeliveryError(TelemetryError):
    """Raised when data cannot reach the remote destination (e.g., Grafana REST API down)."""
    pass

class TelemetrySerializationError(TelemetryError):
    """Raised when a protocol outputs an unserializable object (like a raw socket) into the context."""
    pass


# ==========================================
# DATA CONTRACTS
# ==========================================
@dataclass(frozen=True)
class StationContext:
    """
    Immutable Factory Metadata.
    Identifies WHO is testing, WHAT is being tested, and WHERE it is happening.
    """
    jig_id: str
    operator_id: str
    # These two might be injected later in the test if the DUT is scanned mid-setup,
    # but defining them here sets the structural contract.
    dut_serial: str = "PENDING"
    firmware_version: str = "UNKNOWN"

    facility: Optional[str] = None
    environment: str = "production"
    run_id: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f"))


@dataclass
class TestRecord:
    """
    The Ultimate Telemetry Payload.
    Fuses the Factory Metadata with the physical hardware evaluation.
    Not frozen, because the Pytest setup/call/teardown hooks mutate it.
    """
    # Required fields MUST go first in Python dataclasses
    test_name: str

    # Optional fields with defaults follow
    station_context: Optional[StationContext] = None
    iteration: int = 1
    passed: bool = False
    duration_s: float = 0.0
    error_message: Optional[str] = None
    result: Optional[ValidatorResult] = None

    # Fixed typo: 'field' instead of 'Field'
    timestamp_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Required to support our Automated Forensic Dumps feature!
    context: Dict[str, Any] = field(default_factory=dict)


# ==========================================
# EXPORTER PROTOCOL
# ==========================================
class TelemetryExporter(Protocol):
    """
    Structural contract for all Data Sinks (Local JSONL, InfluxDB, Grafana Loki, MES API).
    """

    def start_session(self, context: StationContext) -> None:
        """
        Initializes the export session.
        Opens file handles, establishes HTTP sessions, or authenticates with DBs.
        """
        ...

    def emit_record(self, record: TestRecord) -> None:
        """
        Synchronously flushes a single test result to the datastore.
        MUST NOT CRASH if the destination is unreachable; should queue or fallback.
        """
        ...

    def end_session(self, session_passed: bool) -> None:
        """
        Finalizes the run.
        Closes DB connections, zips artifacts, or generates the final JSON payload.
        """
        ...
