# src/pytest_mes_core/plugin.py
import pytest
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Generator, Any

from pytest_mes_core.telemetry import TestRecord
from pytest_mes_core.config import StationEnvironment, load_toml_config
from pytest_mes_core.host_adapters.safety import EStopWatchdog
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.transports.serial import EphemeralSerialClient
from pytest_mes_core.transports.failover import FailoverTransport

logger = logging.getLogger("mes_core.plugin")

# Global instance for session lifecycle management (Safety Watchdog)
_global_watchdog = None

# -------------------------------------------------------------------------
# CLI Arguments & Factory UI Integrations
# -------------------------------------------------------------------------
def pytest_addoption(parser: pytest.Parser) -> None:
    """Injects MES-specific CLI arguments into the pytest runner."""
    group = parser.getgroup("mes_core", "Manufacturing Execution System Core")
    group.addoption("--operator-id", action="store", required=True,
                    help="Operator Badge ID (e.g. OP-4092) for ISO 9001 compliance.")
    group.addoption("--env-config", action="store", default="station_env.toml",
                    help="Path to the TOML hardware definition file.")
    parser.addoption("--generate-mes-config", action="store_true",
                     help="Generates a sample TOML configuration file.")
    parser.addoption("--golden-board-mode", action="store_true",
                     help="Executes Daily Jig Verification suite.")

# -------------------------------------------------------------------------
# GLOBAL FIXTURES (THE BOM & TELEMETRY)
# -------------------------------------------------------------------------
@pytest.fixture(scope="session")
def operator_id(request: pytest.FixtureRequest) -> str:
    """Exposes the CLI operator ID to downstream project fixtures."""
    return str(request.config.getoption("--operator-id"))

@pytest.fixture(scope="session")
def station_config(request: pytest.FixtureRequest) -> StationEnvironment:
    """
    THE INDISPUTABLE HARDWARE BOM.
    Parses the TOML environment file into a strict Pydantic model once per session.
    Aborts the entire suite immediately if the facility configuration is malformed.
    """
    toml_path = Path(request.config.getoption("--env-config"))
    logger.info(f"Loading Physical Station Configuration from {toml_path.absolute()}")
    return load_toml_config(toml_path, StationEnvironment)

@pytest.fixture(scope="session")
def telemetry_file() -> Path:
    """Generates the atomic JSONL file path for the current test batch."""
    log_dir = Path("artifacts/telemetry")
    log_dir.mkdir(parents=True, exist_ok=True)
    batch_id = time.strftime("%Y%m%d_%H%M%S")
    return log_dir / f"halt_batch_{batch_id}.jsonl"

# -------------------------------------------------------------------------
# SESSION LIFECYCLE & SAFETY WATCHDOGS
# -------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    """Strips software metadata, injects physical reality, and arms safety watchdogs."""

    if config.getoption("--generate-mes-config"):
        from pytest_mes_core.templates import generate_sample_config
        generate_sample_config()
        pytest.exit("Sample config generated. Exiting.", returncode=0)

    # Arm Safety Watchdogs & Parse Configuration
    toml_path = Path(config.getoption("--env-config"))

    if toml_path.exists():
        try:
            bom = load_toml_config(toml_path, StationEnvironment)

            # 1. Arm E-Stop
            if bom.e_stop:
                global _global_watchdog
                _global_watchdog = EStopWatchdog(bom.e_stop)
                _global_watchdog.start()

            # 2. Generate Forensic Snapshot Artifact
            snapshot_dir = Path("artifacts/forensics")
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot_file = snapshot_dir / f"config_snapshot_{time.strftime('%Y%m%d_%H%M%S')}.json"

            bom_json_str = bom.model_dump_json(indent=2)
            with open(snapshot_file, "w") as f:
                f.write(bom_json_str)

            # 3. HTML Report Metadata Injection
            if hasattr(config, "_metadata"):
                metadata: dict[str, Any] = getattr(config, "_metadata")
                for key in ["Python", "Platform", "Packages", "Plugins"]:
                    metadata.pop(key, None)

                metadata["Facility"] = bom.station_meta.facility
                metadata["Jig ID"] = bom.station_meta.jig_id
                metadata["Test Timestamp (Local)"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                try:
                    metadata["Operator ID"] = config.getoption("--operator-id")
                except ValueError:
                    pass

                metadata["Station Config BOM"] = bom.model_dump_json(exclude_none=True)

        except Exception as e:
            logger.error(f"Failed to load BOM during pytest_configure: {e}")

def pytest_unconfigure(config: pytest.Config) -> None:
    """ZERO-LEAKAGE: Reaps global daemon threads when pytest exits."""
    global _global_watchdog
    if _global_watchdog:
        logger.info("Disarming Physical E-Stop Watchdog daemon...")
        _global_watchdog.stop()

# -------------------------------------------------------------------------
# HTML REPORT HACKING & PASS/FAIL STATE EXTRACTION
# -------------------------------------------------------------------------
def pytest_report_header(config: pytest.Config) -> list[str]:
    """Prints a high-level summary of the active MES Hardware BOM to the terminal."""
    toml_path = Path(config.getoption("--env-config"))
    if not toml_path.exists():
        return [f"MES BOM: FATAL - Missing {toml_path}"]

    try:
        bom = load_toml_config(toml_path, StationEnvironment)
        header = [
            "=",
            f"MES FACILITY : {bom.station_meta.facility}",
            f"JIG ID       : {bom.station_meta.jig_id}",
            f"SAFETY E-STOP: {'[ ARMED ]' if bom.e_stop else '[ DISABLED - DANGEROUS ]'}",
            f"POWER SUPPLY : {bom.psu_hardware.vendor.upper() if bom.psu_hardware else 'None'}",
            "--- ACTIVE HARDWARE INTERFACES ---",
            f"  ETH : {len(bom.ethernet)} | CAN : {len(bom.can_bus)} | UART: {len(bom.uart)}",
            f"  ADC : {len(bom.adc)} | DAC : {len(bom.dac)} | I2C : {len(bom.i2c_eeprom)}",
            f"  GPIO: {len(bom.gpio_edge) + len(bom.gpio_led)} (Edges & LEDs) | Loopbacks: {len(bom.gpio_loopback)}",
            "=",
        ]
        return header
    except Exception as e:
        return [f"MES BOM PARSING ERROR: {e}"]

def pytest_html_report_title(report: Any) -> None:
    report.title = "Manufacturing EOL Certificate"

def pytest_html_results_table_header(cells: list[Any]) -> None:
    cells.insert(2, "<th>Physical Metrics</th>")
    if cells: cells.pop()

def pytest_html_results_table_row(report: Any, cells: list[Any]) -> None:
    metrics_html = getattr(report, "custom_metrics_html", "<i>No data</i>")
    cells.insert(2, f"<td>{metrics_html}</td>")
    if cells: cells.pop()

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, None, None]:
    """Captures the pass/fail state and formats metrics BEFORE teardown executes."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)

    if rep.when == "call":
        metrics_fixture = item.funcargs.get("record_metrics")
        if metrics_fixture and metrics_fixture.metrics:
            html = "<br>".join([f"<b>{k}:</b> {v}" for k, v in metrics_fixture.metrics.items()])
            rep.custom_metrics_html = html

# -------------------------------------------------------------------------
# CORE FIXTURE: ZERO-LEAKAGE TELEMETRY & POST-MORTEM FORENSICS
# -------------------------------------------------------------------------
@pytest.fixture(scope="function")
def record_metrics(
    request: pytest.FixtureRequest,
    telemetry_file: Path,
    operator_id: str,
    station_config: StationEnvironment
) -> Generator[TestRecord, None, None]:
    """
    Dependency Injection for Telemetry.
    Yields the TestRecord to the test logic, performs hardware forensics on failure,
    and guarantees disk flush on teardown.
    """
    callspec = getattr(request.node, "callspec", None)
    iteration = int((callspec.params.get("_pytest_repeat_step_number", 0) + 1) if callspec else 1)

    record = TestRecord(
        test_name=request.node.originalname or request.node.name,
        iteration=iteration,
        operator_id=operator_id
    )

    t0 = time.perf_counter()

    # -------------------------------------------------------------
    # Hand control over to the downstream proprietary hardware test
    yield record
    # -------------------------------------------------------------

    # ZERO-LEAKAGE TEARDOWN: Execute regardless of test panic/timeout/E-Stop
    record.duration_s = round(time.perf_counter() - t0, 3)

    # Scrape both setup and call outcomes
    rep_setup = getattr(request.node, "rep_setup", None)
    rep_call = getattr(request.node, "rep_call", None)

    if rep_setup and rep_setup.failed:
        record.passed = False
        record.error_trace = f"SETUP FAILURE: {str(rep_setup.longrepr)}"
    elif rep_call:
        record.passed = bool(rep_call.passed)
        if rep_call.failed:
            record.error_trace = str(rep_call.longrepr)

            # ==========================================
            # AUTOMATED HARDWARE FORENSIC DUMPER
            # ==========================================
            dut_ssh = request.node.funcargs.get("dut_ssh")
            if dut_ssh and dut_ssh.is_alive():
                commands_to_run = []
                for profile_name, cmds in station_config.post_mortem_dumps.items():
                    if profile_name in record.test_name:
                        commands_to_run.extend(cmds)

                if commands_to_run:
                    logger.warning(f"[Post-Mortem] Test {record.test_name} failed. Executing dumps...")
                    dump_dir = Path("artifacts/forensics")
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    dump_file = dump_dir / f"fail_{record.test_name}_{int(time.time())}.log"

                    try:
                        with open(dump_file, "w") as f:
                            f.write(f"=== POST-MORTEM FORENSICS FOR {record.test_name} ===\n")
                            for cmd in commands_to_run:
                                f.write(f"\n>>> COMMAND: {cmd}\n")
                                res = dut_ssh.safe_run(cmd, timeout_s=5.0)
                                f.write(res.stdout if res.ok else f"ERROR: {res.stderr}\n")

                        record.forensic_dump_path = str(dump_file)
                        logger.info(f"[Post-Mortem] Forensic data saved to {dump_file}")
                    except Exception as e:
                        logger.error(f"[Post-Mortem] Forensic dump failed: {e}")
    else:
        record.passed = False
        record.error_trace = "UNKNOWN FAILURE: Test execution interrupted before call phase."

    # Atomically write to disk
    try:
        record.flush_to_jsonl(telemetry_file)
    except Exception as e:
        logger.critical(f"FATAL: Failed to flush telemetry for {record.test_name}: {e}")
        raise

@pytest.fixture(scope="session")
def raw_ssh(station_config):
    """Base high-speed transport."""
    client = EphemeralSSHClient(station_config.dut_ip)
    client.connect()
    yield client
    client.disconnect()

@pytest.fixture(scope="session")
def raw_serial(station_config):
    """Base out-of-band transport."""
    client = EphemeralSerialClient(port=station_config.serial_port)
    client.connect()
    yield client
    client.disconnect()

@pytest.fixture(scope="function")
def dut(request, raw_ssh, raw_serial):
    """
    THE MASTER ROUTER:
    Yields the optimal transport configuration based on the test's intent.
    """
    # STRATEGY 1: Proactive Routing
    # If the test is tagged with @pytest.mark.destructive_net, hand it the Serial port immediately.
    # We do NOT use the Failover wrapper here, because we know SSH will die and we don't
    # want to waste 30 seconds waiting for the primary transport to timeout.
    if "destructive_net" in request.keywords:
        logger.info("[Router] Destructive network test detected. Pre-routing to Out-of-Band Serial.")
        yield raw_serial
        return

    # STRATEGY 2: Reactive Failover
    # For all normal tests, we provide the High-Speed SSH, but wrapped in the Failover armor.
    # If the board panics mid-test, the wrapper catches it and shifts to Serial invisibly.
    logger.debug("[Router] Standard test detected. Deploying SSH with Serial Failover Matrix.")

    matrix = FailoverTransport(primary=raw_ssh, fallback=raw_serial)
    yield matrix

    # Optional: If the wrapper failed over during the test, we might want to log a severe
    # warning or attempt to recover the SSH pipe for the next test.
    if matrix.is_failed_over:
        logger.warning("[Router] ⚠️ Test completed, but primary transport was permanently lost.")
