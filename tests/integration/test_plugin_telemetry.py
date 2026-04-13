# tests/integration/test_plugin_telemetry.py
import json
from pathlib import Path

def test_telemetry_atomic_flush_on_fatal_crash(pytester):
    """
    META-TEST: Spawns a sub-pytest process. Proves that if a proprietary
    test violently crashes, the mes_record fixture still flushes the JSONL.
    """
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
    pytester.makepyfile("""
def test_evse_high_voltage_crash(mes_record):
    mes_record.metrics["inrush_current_a"] = 120.5
    x = 1 / 0  # FATAL PYTHON CRASH (ZeroDivisionError)
""")

    # 3. Execute Pytest as an Operator would on the factory floor
    # CRITICAL: We pass "-p pytest_mes_core.plugin" to explicitly load our plugin
    # in the subprocess, since we disabled it globally in pyproject.toml!
    result = pytester.runpytest(
        "-p", "pytest_mes_core.plugin",
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
