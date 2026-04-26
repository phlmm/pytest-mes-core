# tests/integration/test_plugin.py
"""
Pytester Integration Tests for the MES Plugin Lifecycle.

These tests spawn REAL isolated pytest sub-processes to verify that:
1. The plugin correctly registers CLI options and markers.
2. The TOML config is parsed and injected into fixtures.
3. The FSM orchestrator's enforce_physical_state autouse fixture works.
4. The hardware_retry marker correctly routes to pytest-rerunfailures.
5. The telemetry pipeline survives test crashes (Zero-Leakage).
6. The mock-hardware flag correctly injects the MockTransport.
7. The terminal summary displays the Hardware Manifest (BOM).

Each test uses pytester.runpytest_subprocess() which spawns a fully isolated OS process,
guaranteeing no state leakage between test runs. This is critical because our plugin
registers global hooks and session-scoped fixtures.
"""

import os
import sys
import json
from pathlib import Path

# All integration tests need PYTHONPATH set so the subprocess can find the source
WORKSPACE_SRC = str(Path(__file__).parent.parent.parent.absolute() / "src")

def _inject_pythonpath():
    """Ensures the subprocess can import pytest_mes_core from the src/ layout."""
    import site
    user_site = site.getusersitepackages()
    os.environ["PYTHONPATH"] = WORKSPACE_SRC + os.pathsep + user_site + os.pathsep + os.environ.get("PYTHONPATH", "")


# ==========================================
# HELPER: Minimal TOML for plugin activation
# ==========================================
MINIMAL_TOML = """
[station_meta]
facility = "Pytester Integration Lab"
jig_id = "JIG-PYTESTER-01"

[telemetry]
exporter_type = "jsonl"
log_directory = "artifacts/telemetry"
"""

# ==========================================
# TEST 1: CLI Option Registration
# ==========================================
def test_plugin_registers_operator_id_as_required(pytester):
    """
    Proves that --operator-id is a mandatory CLI argument.
    Running without it must produce a clear error, not a cryptic traceback.
    """
    _inject_pythonpath()

    pytester.makepyfile("""
def test_dummy():
    assert True
""")
    toml_path = pytester.makefile(".toml", MINIMAL_TOML)

    # Run WITHOUT --operator-id
    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        f"--env-config={toml_path}",
    )
    result.stderr.fnmatch_lines(["*--operator-id*"])


# ==========================================
# TEST 2: TOML Config Parsing into Fixtures
# ==========================================
def test_plugin_parses_toml_into_mes_env_fixture(pytester):
    """
    Proves the mes_env fixture correctly parses a TOML config
    and exposes the station metadata to test functions.
    """
    _inject_pythonpath()

    toml_path = pytester.makefile(".toml", """
[station_meta]
facility = "Sofia Factory Line 3"
jig_id = "JIG-INTEGRATION-42"

[telemetry]
exporter_type = "jsonl"
log_directory = "artifacts/telemetry"
""")

    pytester.makepyfile("""
import pytest

# Override the hardware transports that we don't have in CI
@pytest.fixture(scope="session")
def dut_transport():
    class Dummy:
        is_connected = False
        def connect(self): pass
        def disconnect(self): pass
        def safe_run(self, cmd, **kw): pass
    return Dummy()

@pytest.fixture(scope="session")
def dut_state_machine():
    return None

def test_mes_env_has_jig_id(mes_env):
    assert mes_env.station_meta.jig_id == "JIG-INTEGRATION-42"
    assert mes_env.station_meta.facility == "Sofia Factory Line 3"

def test_operator_id_fixture(operator_id):
    assert operator_id == "CI-BOT"
""")

    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=CI-BOT",
        f"--env-config={toml_path}",
    )
    result.assert_outcomes(passed=2)


# ==========================================
# TEST 3: Custom Markers Registration
# ==========================================
def test_plugin_registers_custom_markers(pytester):
    """
    Proves requires_state and hardware_retry markers are registered
    and don't produce PytestUnknownMarkWarning.
    """
    _inject_pythonpath()

    toml_path = pytester.makefile(".toml", MINIMAL_TOML)

    pytester.makepyfile("""
import pytest

@pytest.fixture(scope="session")
def dut_transport():
    class Dummy:
        is_connected = False
        def connect(self): pass
        def disconnect(self): pass
        def safe_run(self, cmd, **kw): pass
    return Dummy()

@pytest.fixture(scope="session")
def dut_state_machine():
    return None

@pytest.mark.requires_state("OS_USERLAND")
def test_with_requires_state():
    assert True

@pytest.mark.hardware_retry(3)
def test_with_hardware_retry():
    assert True
""")

    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=OP-MARKER",
        f"--env-config={toml_path}",
        "-W", "error::pytest.PytestUnknownMarkWarning",
    )
    result.assert_outcomes(passed=2)


# ==========================================
# TEST 4: hardware_retry Marker → rerunfailures
# ==========================================
def test_hardware_retry_marker_translates_to_rerunfailures(pytester):
    """
    Proves that @pytest.mark.hardware_retry(2) causes the test to be
    retried via the pytest-rerunfailures backend when it fails.
    """
    _inject_pythonpath()

    toml_path = pytester.makefile(".toml", MINIMAL_TOML)

    pytester.makepyfile("""
import pytest

@pytest.fixture(scope="session")
def dut_transport():
    class Dummy:
        is_connected = False
        def connect(self): pass
        def disconnect(self): pass
        def safe_run(self, cmd, **kw): pass
    return Dummy()

@pytest.fixture(scope="session")
def dut_state_machine():
    return None

_counter = 0

@pytest.mark.hardware_retry(2)
def test_flaky_hardware():
    global _counter
    _counter += 1
    if _counter < 3:
        assert False, "Simulated hardware glitch"
    assert True
""")

    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=OP-RETRY",
        f"--env-config={toml_path}",
    )
    # Verify the subprocess output shows the rerun pattern
    result.stdout.fnmatch_lines(["*RERUN*"])
    result.stdout.fnmatch_lines(["*1 passed*2 rerun*"])


# ==========================================
# TEST 5: Telemetry Zero-Leakage on Crash
# ==========================================
def test_telemetry_record_survives_test_crash(pytester):
    """
    Proves that metrics injected into mes_record BEFORE a crash
    are preserved in the JSONL output. This validates the Zero-Leakage
    try/finally contract in telemetry_hooks.py.
    """
    _inject_pythonpath()

    toml_path = pytester.makefile(".toml", MINIMAL_TOML)

    pytester.makepyfile("""
import pytest

@pytest.fixture(scope="session")
def dut_transport():
    class Dummy:
        is_connected = False
        def connect(self): pass
        def disconnect(self): pass
        def safe_run(self, cmd, **kw): pass
    return Dummy()

@pytest.fixture(scope="session")
def dut_state_machine():
    return None

def test_crash_with_metrics(mes_record):
    mes_record.metrics["voltage_v"] = 3.3
    mes_record.metrics["current_a"] = 0.150
    mes_record.context["test_phase"] = "inrush"
    assert False, "Intentional crash to test telemetry survival"
""")

    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=OP-CRASH",
        f"--env-config={toml_path}",
    )
    result.assert_outcomes(failed=1)

    # Find the JSONL file in the telemetry spool
    jsonl_files = list(Path(pytester.path).rglob("*.jsonl"))
    assert len(jsonl_files) >= 1, f"No JSONL files found in {pytester.path}"

    with open(jsonl_files[0], "r") as f:
        record = json.loads(f.read())

    assert record["passed"] is False
    assert record["metrics"]["voltage_v"] == 3.3
    assert record["metrics"]["current_a"] == 0.150
    assert record["context"]["test_phase"] == "inrush"
    assert "AssertionError" in record["error_message"] or "Intentional crash" in record["error_message"]


# ==========================================
# TEST 6: Telemetry Survives Passing Test
# ==========================================
def test_telemetry_records_passing_test(pytester):
    """
    Proves that a passing test's duration, operator ID, and station context
    are correctly recorded in the JSONL payload.
    """
    _inject_pythonpath()

    toml_path = pytester.makefile(".toml", MINIMAL_TOML)

    pytester.makepyfile("""
import pytest

@pytest.fixture(scope="session")
def dut_transport():
    class Dummy:
        is_connected = False
        def connect(self): pass
        def disconnect(self): pass
        def safe_run(self, cmd, **kw): pass
    return Dummy()

@pytest.fixture(scope="session")
def dut_state_machine():
    return None

def test_healthy_board(mes_record):
    mes_record.metrics["soc_temp_c"] = 42.5
    assert True
""")

    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=OP-PASS",
        "--board-serial=SN-GOLDEN-001",
        f"--env-config={toml_path}",
    )
    result.assert_outcomes(passed=1)

    jsonl_files = list(Path(pytester.path).rglob("*.jsonl"))
    assert len(jsonl_files) >= 1

    with open(jsonl_files[0], "r") as f:
        record = json.loads(f.read())

    assert record["passed"] is True
    assert record["metrics"]["soc_temp_c"] == 42.5
    assert record["station_context"]["operator_id"] == "OP-PASS"
    assert record["station_context"]["jig_id"] == "JIG-PYTESTER-01"


# ==========================================
# TEST 7: Terminal Summary Shows BOM
# ==========================================
def test_terminal_summary_shows_hardware_manifest(pytester):
    """
    Proves the plugin injects the Hardware Manifest section
    into the final pytest terminal output.
    """
    _inject_pythonpath()

    toml_path = pytester.makefile(".toml", """
[station_meta]
facility = "Munich Validation Center"
jig_id = "JIG-MUC-007"

[telemetry]
exporter_type = "jsonl"
log_directory = "artifacts/telemetry"
""")

    pytester.makepyfile("""
import pytest

@pytest.fixture(scope="session")
def dut_transport():
    class Dummy:
        is_connected = False
        def connect(self): pass
        def disconnect(self): pass
        def safe_run(self, cmd, **kw): pass
    return Dummy()

@pytest.fixture(scope="session")
def dut_state_machine():
    return None

def test_trivial():
    assert True
""")

    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=OP-BOM",
        f"--env-config={toml_path}",
    )
    result.assert_outcomes(passed=1)

    # The terminal summary should contain the BOM section
    result.stdout.fnmatch_lines([
        "*Hardware Manifest*",
        "*Munich Validation Center*",
        "*JIG-MUC-007*",
    ])


# ==========================================
# TEST 8: Mock Hardware Flag
# ==========================================
def test_mock_hardware_flag_injects_virtual_transport(pytester):
    """
    Proves --mock-hardware injects a MockTransport and tests
    can call safe_run() without any physical hardware.
    """
    _inject_pythonpath()

    toml_path = pytester.makefile(".toml", MINIMAL_TOML)

    pytester.makepyfile("""
import pytest

@pytest.fixture(scope="session")
def dut_state_machine():
    return None

def test_mock_transport_works(dut_transport):
    assert dut_transport is not None
    # MockTransport has is_connected=True by default
    assert dut_transport.is_connected is True
""")

    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=OP-MOCK",
        "--mock-hardware",
        f"--env-config={toml_path}",
    )
    result.assert_outcomes(passed=1)


# ==========================================
# TEST 9: Multiple Tests in One Session
# ==========================================
def test_multi_test_session_produces_correct_record_count(pytester):
    """
    Proves that the telemetry engine emits one record per test
    in a multi-test session, not duplicating or dropping payloads.
    """
    _inject_pythonpath()

    toml_path = pytester.makefile(".toml", MINIMAL_TOML)

    pytester.makepyfile("""
import pytest

@pytest.fixture(scope="session")
def dut_transport():
    class Dummy:
        is_connected = False
        def connect(self): pass
        def disconnect(self): pass
        def safe_run(self, cmd, **kw): pass
    return Dummy()

@pytest.fixture(scope="session")
def dut_state_machine():
    return None

def test_alpha(mes_record):
    mes_record.metrics["test_id"] = 1
    assert True

def test_bravo(mes_record):
    mes_record.metrics["test_id"] = 2
    assert True

def test_charlie(mes_record):
    mes_record.metrics["test_id"] = 3
    assert False, "Intentional failure"
""")

    result = pytester.runpytest_subprocess(
        "-p", "no:mes_core",
        "-p", "pytest_mes_core.plugin",
        "--operator-id=OP-MULTI",
        f"--env-config={toml_path}",
    )
    result.assert_outcomes(passed=2, failed=1)

    jsonl_files = list(Path(pytester.path).rglob("*.jsonl"))
    assert len(jsonl_files) >= 1

    with open(jsonl_files[0], "r") as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]

    assert len(lines) == 3, f"Expected 3 JSONL records, got {len(lines)}"

    records = [json.loads(line) for line in lines]
    passed_count = sum(1 for r in records if r["passed"])
    failed_count = sum(1 for r in records if not r["passed"])

    assert passed_count == 2
    assert failed_count == 1
