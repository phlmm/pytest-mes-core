# src/pytest_mes_core/telemetry/base.py
import time
from datetime import datetime, timezone
from typing import Dict, Any, Protocol, Optional, Union
from pydantic import BaseModel, Field, ConfigDict, model_validator
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
class StationContext(BaseModel):
    """
    Immutable Factory Metadata.
    Identifies WHO is testing, WHAT is being tested, and WHERE it is happening.
    """
    jig_id: str
    operator_id: str

    # --- DUT Identity & Genealogy ---
    dut_serial: str = "PENDING"
    work_order: str = "UNKNOWN"
    firmware_version: str = "UNKNOWN"

    # The Hardware BOM/Manifest of the specific board being tested
    dut_manifest: Dict[str, Any] = Field(default_factory=dict)
    
    # The OS / Software layer versions of the board
    software_manifest: Dict[str, Any] = Field(default_factory=dict)

    facility: Optional[str] = None
    environment: str = "lab"
    run_id: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f"))

class TestRecord(BaseModel):
    """
    The Ultimate Telemetry Payload.
    Fuses the Factory Metadata with the physical hardware evaluation.
    """
    # Allow arbitrary types so Pytest doesn't crash if an engineer injects a weird object
    model_config = ConfigDict(arbitrary_types_allowed=True)

    test_name: str
    passed: bool = False
    iteration: int = 1
    duration_s: float = 0.0
    error_message: Optional[str] = None

    # THE BLOAT FIX: Keep in memory, but hide from JSON
    result: Optional[ValidatorResult] = Field(default=None, exclude=True)

    station_context: Optional[StationContext] = None
    metrics: Dict[str, Union[float, int, str]] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    timestamp_utc: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode='after')
    def _sanitize_context(self) -> 'TestRecord':
        """
        Safety net: Stringifies any non-standard Python objects in the context dictionary
        to prevent Pydantic serialization crashes during the JSONL dump.
        """
        safe_context = {}
        for k, v in self.context.items():
            # Allow primitives and basic structures
            if isinstance(v, (str, int, float, bool, type(None), list, dict)):
                safe_context[k] = v
            else:
                # Force complex objects (Exceptions, Sockets, etc.) to strings
                safe_context[k] = str(v)
        self.context = safe_context
        return self

    def absorb(self, validator_res: ValidatorResult, prefix: str = "") -> None:
        """Surgically flattens a ValidatorResult into the JSONL record.

        Args:
            validator_res: The validation result to absorb.
            prefix: An optional prefix to prepend to metrics and context keys.
        """
        self.result = validator_res
        pfx = f"{prefix}_" if prefix else ""
        self.metrics.update({f"{pfx}{k}": v for k, v in validator_res.metrics.items()})
        self.context.update({f"{pfx}{k}": v for k, v in validator_res.context.items()})
        if not validator_res.passed and validator_res.error_msg:
            self.context[f"{pfx}error"] = validator_res.error_msg

# ==========================================
# EXPORTER PROTOCOL
# ==========================================
class TelemetryExporter(Protocol):
    """
    Structural contract for all Data Sinks (Local JSONL, InfluxDB, Grafana Loki, MES API).
    """
    @property
    def context(self) -> Optional[StationContext]: ...

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
