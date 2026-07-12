import anyio
# src/pytest_mes_core/telemetry/base.py
import time
from datetime import datetime, timezone
from typing import Dict, Any, Protocol, Optional, Union, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator
from pytest_mes_core.protocols.base import ValidatorResult


def _coerce_json_safe(value: Any) -> Any:
    """Recursively coerce a value into something json-serializable.

    Keeps primitives (str/int/float/bool/None) as-is, recurses into
    lists/dicts to catch nested non-serializable objects, and stringifies
    anything else (Exceptions, sockets, arbitrary objects, etc.).
    """
    if isinstance(value, dict):
        return {k: _coerce_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return str(value)

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
    outcome: Literal["passed", "failed", "skipped", "unknown"] = "unknown"
    iteration: int = 1
    duration_s: float = 0.0
    error_message: Optional[str] = None

    # THE BLOAT FIX: Keep in memory, but hide from JSON
    result: Optional[ValidatorResult] = Field(default=None, exclude=True)

    station_context: Optional[StationContext] = None
    metrics: Dict[str, Union[float, int, str]] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    timestamp_utc: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def sanitize(self) -> None:
        """Coerce non-serializable objects in context/metrics to strings.

        Must be called immediately before serialization: context and metrics
        are mutated freely after construction (fixtures, absorb(), tests), so
        the construction-time validator alone cannot guarantee a dumpable
        record.
        """
        self.context = {k: _coerce_json_safe(v) for k, v in self.context.items()}
        self.metrics = {k: _coerce_json_safe(v) for k, v in self.metrics.items()}

    @model_validator(mode='after')
    def _sanitize_context(self) -> 'TestRecord':
        """
        Safety net: Stringifies any non-standard Python objects in the context/
        metrics dictionaries to prevent Pydantic serialization crashes during
        the JSONL dump. Also re-run via sanitize() immediately before
        serialization, since both dicts are mutated freely post-construction.
        """
        self.sanitize()
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

    async def async_absorb(self, validator_res: ValidatorResult, prefix: str = "") -> None:
        return await anyio.to_thread.run_sync(self.absorb, validator_res, prefix)

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

    async def async_start_session(self, context: StationContext) -> None: ...

    def emit_record(self, record: TestRecord) -> None:
        """
        Synchronously flushes a single test result to the datastore.
        MUST NOT CRASH if the destination is unreachable; should queue or fallback.
        """
        ...

    async def async_emit_record(self, record: TestRecord) -> None: ...

    def end_session(self, session_passed: bool) -> None:
        """
        Finalizes the run.
        Closes DB connections, zips artifacts, or generates the final JSON payload.
        """
        ...

    async def async_end_session(self, session_passed: bool) -> None: ...
