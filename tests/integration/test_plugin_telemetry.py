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

    import os
    import sys
    from pathlib import Path
    workspace_root = str(Path(__file__).parent.parent.parent.absolute() / "src")
    os.environ["PYTHONPATH"] = workspace_root + os.pathsep + os.pathsep.join(sys.path)

    # 3. Execute Pytest in a completely isolated OS process
    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=OP-123",
        f"--env-config={toml_path}"
    )

    # Assert the sub-process failed (as expected)
    result.assert_outcomes(failed=1)

    # 4. Mathematically prove the Telemetry Engine locked and flushed the file
    log_dir = Path(pytester.path) / "artifacts" / "telemetry"
    jsonl_files = list(log_dir.rglob("*.jsonl"))
    assert len(jsonl_files) == 1, "JSONL file was not created!"

    with open(jsonl_files[0], "r") as f:
        data = json.loads(f.read())
        assert "test_evse_high_voltage_crash" in data["test_name"]
        assert data["passed"] is False
        assert "ZeroDivisionError" in data["error_message"]
        # CRITICAL: Proves context recorded BEFORE the crash survived the generator teardown!
        assert data["metrics"]["inrush_current_a"] == 120.5
        assert data["station_context"]["operator_id"] == "OP-123"


def test_telemetry_runtime_skip_is_not_recorded_as_failure(pytester):
    """Fix 2: a runtime pytest.skip() (e.g. "requires a physical PSU") must be
    recorded with outcome="skipped" everywhere, NOT as a failure."""
    # Use a non-lab/developer environment so the OperatorReceiptExporter (not
    # the markdown exporter) is wired into the composite sink.
    toml_path = pytester.makefile(".toml", """
[station_meta]
facility = "Sofia (EMS Line 1)"
jig_id = "JIG-99"
environment = "production"

[telemetry]
exporter_type = "jsonl"
log_directory = "artifacts/telemetry"
""")

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

def test_evse_passes(mes_record):
    assert 1 == 1

def test_evse_requires_psu(mes_record):
    pytest.skip("no PSU")
""")

    import os
    import sys
    from pathlib import Path
    workspace_root = str(Path(__file__).parent.parent.parent.absolute() / "src")
    os.environ["PYTHONPATH"] = workspace_root + os.pathsep + os.pathsep.join(sys.path)

    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=OP-123",
        f"--env-config={toml_path}"
    )

    result.assert_outcomes(passed=1, skipped=1)

    log_dir = Path(pytester.path) / "artifacts" / "telemetry"
    jsonl_files = list(log_dir.rglob("*.jsonl"))
    assert len(jsonl_files) == 1, "JSONL file was not created!"

    with open(jsonl_files[0], "r") as f:
        records = [json.loads(line) for line in f if line.strip()]

    by_name = {r["test_name"].split("::")[-1]: r for r in records}
    assert by_name["test_evse_passes"]["outcome"] == "passed"
    assert by_name["test_evse_requires_psu"]["outcome"] == "skipped"
    assert by_name["test_evse_requires_psu"]["passed"] is False

    receipt_files = list(log_dir.rglob("*.txt"))
    assert len(receipt_files) == 1, "Operator receipt was not created!"
    receipt_body = receipt_files[0].read_text()
    assert "Failed Tests : 0" in receipt_body
    assert "Skipped Tests : 1" in receipt_body
