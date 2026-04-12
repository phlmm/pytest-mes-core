from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

class ValidatorResult(BaseModel):
    """
    Standardized, immutable return contract for all MES Hardware Validators.
    Guarantees strict type safety before JSONL telemetry serialization.
    """
    passed: bool
    metrics: Dict[str, float] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    error_msg: str = ""

    # Defensive helper to format console output
    def format_trace(self) -> str:
        status = "PASS" if self.passed else f"FAIL ({self.error_msg})"
        return f"[{status}] Metrics: {self.metrics}"
