# src/pytest_mes_core/telemetry.py
import os
import time
import fcntl
import logging
from typing import Dict, Any, Optional, TYPE_CHECKING
from pathlib import Path
from pydantic import BaseModel, Field

# Prevent circular imports while maintaining strict type hints
if TYPE_CHECKING:
    from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.telemetry")

class TestRecord(BaseModel):
    """
    Immutable factory record of a single test execution.
    Designed for flat NoSQL/JSONL ingestion (Grafana/Pandas).
    """
    timestamp_unix: float = Field(default_factory=time.time)
    test_name: str
    iteration: int = Field(default=1, gt=0)
    operator_id: str
    duration_s: float = Field(default=0.0, ge=0.0)
    passed: bool = False

    # Quantitative physics measurements (e.g., {"idle_power_w": 4.2})
    metrics: Dict[str, float] = Field(default_factory=dict)

    # Qualitative context (e.g., {"jig_id": "JIG-01", "psu_vendor": "keysight"})
    hardware_context: Dict[str, Any] = Field(default_factory=dict)

    # Tracebacks for RMAs
    error_trace: Optional[str] = None
    forensic_dump_path: Optional[str] = None

    def flush_to_jsonl(self, filepath: Path) -> None:
        """
        Atomically appends the minified record to disk to survive hard crashes.
        Enforces strict OS-level locks and hardware syncs.
        """
        filepath.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump_json(exclude_none=True) + "\n"

        try:
            with open(filepath, "a", encoding="utf-8") as f:
                # 1. Blocking OS-level lock
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
                    fcntl.flock(f, fcntl.LOCK_UN)

        except OSError as e:
            logger.critical(f"[Telemetry] FATAL: Disk/Network write failed on {filepath}: {e}")
            self._execute_emergency_dump(payload)
            raise RuntimeError(f"Telemetry flush failed! Disk full? Emergency dump attempted.")

    def _execute_emergency_dump(self, payload: str) -> None:
        """Attempts to save data to the volatile RAM disk if the main drive drops."""
        fallback_file = Path(f"/tmp/mes_emergency_dump_{int(time.time())}.jsonl")
        logger.critical(f"[Telemetry] Executing emergency dump to {fallback_file}")

        try:
            with open(fallback_file, "a", encoding="utf-8") as fb:
                fb.write(payload)
                fb.flush()
                os.fsync(fb.fileno())
        except OSError as e:
            # If /tmp is full (e.g., out of RAM), we are completely dead.
            # Log it so at least journalctl catches the telemetry string.
            logger.critical(f"[Telemetry] TOTAL CATASTROPHE. Emergency dump failed: {e}")
            logger.critical(f"[Telemetry] RAW PAYLOAD: {payload}")

    def absorb(self, result: 'ValidatorResult', prefix: str = "") -> None:
        """
        Instantly dumps validator physics into the JSONL Telemetry record.
        Safely aggregates multiple errors if a looped test yields multiple failures.
        """
        pfx = f"{prefix}_" if prefix else ""

        # 1. Absorb Metrics safely
        for k, v in result.metrics.items():
            full_key = f"{pfx}{k}"
            if full_key in self.metrics:
                logger.warning(f"[Telemetry] Metric collision! Overwriting '{full_key}': {self.metrics[full_key]} -> {v}")
            self.metrics[full_key] = v

        # 2. Absorb Context safely
        for k, v in result.context.items():
            full_key = f"{pfx}{k}"
            if full_key in self.hardware_context:
                logger.warning(f"[Telemetry] Context collision! Overwriting '{full_key}': {self.hardware_context[full_key]} -> {v}")
            self.hardware_context[full_key] = v

        # 3. Flatten and aggregate error messages intelligently
        if not result.passed and result.error_msg:
            err_key = f"{pfx}error"
            if err_key in self.hardware_context:
                # Append instead of overwrite so we don't lose the first failure reason
                self.hardware_context[err_key] += f" | {result.error_msg}"
            else:
                self.hardware_context[err_key] = result.error_msg
