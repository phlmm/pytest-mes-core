# src/pytest_mes_core/telemetry/jsonl_exporter.py
import os
import json
import time
import logging
import dataclasses
from enum import Enum
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # Graceful fallback for developers testing on Windows PCs

from pytest_mes_core.telemetry.base import (
    StationContext,
    TestRecord,
    TelemetryDeliveryError,
    TelemetrySerializationError
)

logger = logging.getLogger("mes_core.telemetry.jsonl")

class MesTelemetryEncoder(json.JSONEncoder):
    """
    Advanced JSON encoder that gracefully serializes Python-specific constructs
    (Enums, bytes, generic Dataclasses) that often end up in the `context` dictionary.
    """
    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, bytes):
            # Attempt UTF-8, fallback to hex string to prevent ugly "b'...'" strings
            try:
                return obj.decode('utf-8')
            except UnicodeDecodeError:
                return obj.hex()
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        if hasattr(obj, "model_dump"): # Support Pydantic models
            return obj.model_dump()

        # Absolute fallback: cast to string, but strip out the memory addresses
        # e.g., "<MyObject object at 0x7f8b...>" -> "MyObject"
        str_val = str(obj)
        if str_val.startswith("<") and " object at " in str_val:
            return obj.__class__.__name__

        return str_val


class JsonlTelemetryExporter:
    """
    Local Disk Telemetry Sink (Grafana/Promtail compatible).
    Atomically appends minified records to disk to survive hard crashes.
    Enforces strict OS-level locks and hardware syncs.
    """
    def __init__(self, log_directory: Path):
        self.log_dir = Path(log_directory)
        self.active_file: Path | None = None
        self.context: StationContext | None = None

    # ==========================================
    # TELEMETRY EXPORTER CONTRACT
    # ==========================================
    def start_session(self, context: StationContext) -> None:
        """Initializes the session and dynamically generates the file path."""
        self.context = context
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # File format: JIG-01_20260413_104330.jsonl
        filename = f"{context.jig_id}_{context.run_id}.jsonl"
        self.active_file = self.log_dir / filename

        logger.info(f"[Telemetry] Session armed. Streaming to {self.active_file}")

    def emit_record(self, record: TestRecord) -> None:
        """Serializes and flushes a single payload to the active file."""
        if not self.active_file:
            raise TelemetryDeliveryError("Attempted to emit record before starting session.")

        # 1. Defensive Serialization Shield
        try:
            raw_dict = dataclasses.asdict(record)
            # Use our custom encoder to ensure clean, Grafana-ready JSON
            payload_str = json.dumps(raw_dict, cls=MesTelemetryEncoder) + "\n"
        except Exception as e:
            logger.error(f"[Telemetry] Fatal JSON Serialization failure: {e}")
            raise TelemetrySerializationError(f"Failed to serialize record for {record.test_name}")

        # 2. Atomic Physical Write
        try:
            self._atomic_append(self.active_file, payload_str)
        except OSError as e:
            logger.critical(f"[Telemetry] FATAL: Disk write failed on {self.active_file}: {e}")
            self._execute_emergency_dump(payload_str)

            # We raise the Domain Exception so the master framework knows the telemetry backend is crippled
            raise TelemetryDeliveryError("Telemetry flush failed! Disk full? Emergency dump attempted.")

    def end_session(self, session_passed: bool) -> None:
        """Finalizes the run. (JSONL does not require EOF markers, so we just log it)."""
        logger.info(f"[Telemetry] Session finalized. Overall Result: {'PASS' if session_passed else 'FAIL'}")
        self.active_file = None
        self.context = None

    # ==========================================
    # INTERNAL HARDWARE I/O HELPERS
    # ==========================================
    def _atomic_append(self, filepath: Path, payload: str) -> None:
        """Writes data to the physical silicon with extreme paranoia."""
        with open(filepath, "a", encoding="utf-8") as f:
            # 1. Blocking OS-level lock (prevents overlapping writes if parallel Pytest workers are running)
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)

            try:
                # 2. Write to Python buffer
                f.write(payload)
                # 3. Flush to OS Page Cache
                f.flush()
                # 4. DEFENSIVE: Force OS to write Page Cache to physical silicon
                os.fsync(f.fileno())
            finally:
                # 5. Guarantee lock release even if the disk errors out during write
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def _execute_emergency_dump(self, payload: str) -> None:
        """Attempts to save data to the volatile RAM disk if the main drive drops."""
        fallback_file = Path(f"/tmp/mes_emergency_dump_{int(time.time())}.jsonl")
        logger.critical(f"[Telemetry] Executing emergency dump to {fallback_file}")

        try:
            # We don't bother locking /tmp, we just need the data to survive
            with open(fallback_file, "a", encoding="utf-8") as fb:
                fb.write(payload)
                fb.flush()
                os.fsync(fb.fileno())
        except OSError as e:
            # If /tmp is full (e.g., Out of RAM), we are completely dead.
            # Log it so at least journalctl catches the telemetry string.
            logger.critical(f"[Telemetry] TOTAL CATASTROPHE. Emergency dump failed: {e}")
            logger.critical(f"[Telemetry] RAW PAYLOAD: {payload}")
