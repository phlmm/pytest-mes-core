# tests/integration/test_plugin_telemetry.py
import json
from pathlib import Path

def test_telemetry_atomic_flush_on_fatal_crash(pytester):
    # 1. Create a pristine, isolated factory BOM
    toml_path = pytester.makefile(".toml", """
[station_meta]
facility = "Sofia (EMS Line 1)"
jig_id = "JIG-99"

[telemetry]
exporter_type = "jsonl"
log_directory = "artifacts/telemetry"
""")

    # 2. Write a dummy proprietary EVSE test that crashes instantly
    # We inject a dummy dut_transport so the setup doesn't skip looking for hardware!
    pytester.makepyfile("""
import pytest

@pytest.fixture(scope="session")
def dut_transport():
    class DummyTransport:
        @property
        def is_connected(self):
            return False
        def connect(self): pass
        def disconnect(self): pass
        def safe_run(self, cmd, timeout_s=5.0): pass
    return DummyTransport()

def test_evse_high_voltage_crash(mes_record):
    mes_record.metrics["inrush_current_a"] = 120.5
    x = 1 / 0  # FATAL PYTHON CRASH (ZeroDivisionError)
""")

    # 3. Execute Pytest in a completely isolated OS process
    result = pytester.runpytest_subprocess(
        "--operator-id=OP-123",
        f"--env-config={toml_path}"
    )

    # Assert the sub-process failed (as expected)
    result.assert_outcomes(failed=1)

    # 4. Mathematically prove the Telemetry Engine locked and flushed the file
    log_dir = Path(pytester.path) / "artifacts" / "telemetry"
    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1, "JSONL file was not created!"

    with open(jsonl_files[0], "r") as f:
        data = json.loads(f.read())
        assert data["test_name"] == "test_evse_high_voltage_crash"
        assert data["passed"] is False
        assert "ZeroDivisionError" in data["error_message"]
        # CRITICAL: Proves context recorded BEFORE the crash survived the generator teardown!
        assert data["metrics"]["inrush_current_a"] == 120.5
        assert data["station_context"]["operator_id"] == "OP-123"
