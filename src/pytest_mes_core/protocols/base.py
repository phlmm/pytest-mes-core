from typing import Dict, Any
from pydantic import BaseModel, Field, ConfigDict, model_validator

# ==========================================
# LAYER 3: APPLICATION TELEMETRY
# ==========================================
class ValidatorResult(BaseModel):
    """
    Standardized, strictly IMMUTABLE return contract for all MES Hardware Validators.
    Guarantees logical consistency and type safety before JSONL telemetry serialization.
    """

    # 1. Enforce Absolute Immutability and prevent arbitrary attribute injection
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    metrics: Dict[str, float] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    error_msg: str = Field(default="")

    # 2. Enforce Hardware Logic Rules
    @model_validator(mode='after')
    def validate_logical_consistency(self) -> 'ValidatorResult':
        """Defends against contradictory test states."""

        if self.passed and self.error_msg:
            raise ValueError(
                f"Contradictory State: Test marked as PASSED, but contains error message: '{self.error_msg}'"
            )

        if not self.passed and not self.error_msg.strip():
            raise ValueError(
                "Forensic Violation: Test marked as FAILED, but no error_msg was provided. "
                "Hardware failures must include a diagnostic reason."
            )

        return self

    def format_trace(self) -> str:
        """Defensive helper to format console output without JSONL serialization errors."""
        status = "PASS" if self.passed else f"FAIL ({self.error_msg})"

        # Format metrics cleanly. If empty, return a clean string rather than '{}'
        metrics_str = " | ".join(f"{k}: {v}" for k, v in self.metrics.items())
        return f"[{status}] {metrics_str}".strip()
