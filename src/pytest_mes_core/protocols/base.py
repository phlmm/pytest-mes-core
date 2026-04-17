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

def collect_soc_health(dut) -> Dict[str, float]:
    """Helper to collect physical CPU temperature and clock frequency."""
    health: Dict[str, float] = {}
    try:
        res_temp = dut.safe_run("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null", timeout_s=1.0)
        if res_temp.ok and res_temp.stdout.strip().isdigit():
            # Thermal zone temp is usually in millidegrees Celsius
            health["soc_temp_c"] = round(float(res_temp.stdout.strip()) / 1000.0, 1)

        res_freq = dut.safe_run("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null", timeout_s=1.0)
        if res_freq.ok and res_freq.stdout.strip().isdigit():
            # freq is usually in KHz, convert to MHz
            health["cpu_freq_mhz"] = round(float(res_freq.stdout.strip()) / 1000.0, 1)
    except Exception:
        pass  # Transport dropped or missing sensors
    return health
