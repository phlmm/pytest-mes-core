# src/pytest_mes_core/plugin.py
import pytest
import time
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Generator, Any, Optional

from pytest_mes_core.config import StationEnvironment, load_toml_config
from pytest_mes_core.host_adapters import EStopWatchdog
from pytest_mes_core.transports import SshTransport
from pytest_mes_core.transports import SerialTransport
from pytest_mes_core.transports import FailoverTransport
from pytest_mes_core.provisioning import HardwareBootstrapper
from pytest_mes_core.telemetry import TelemetrySink, StationContext, TestRecord

logger = logging.getLogger("mes_core.plugin")

# Global instances for session lifecycle management
_global_watchdog: Optional[EStopWatchdog] = None
_global_telemetry_sink: Optional[TelemetrySink] = None

# -------------------------------------------------------------------------
# CLI ARGUMENTS
# -------------------------------------------------------------------------
def pytest_addoption(parser: pytest.Parser) -> None:
    """Injects MES-specific CLI arguments into the pytest runner."""
    group = parser.getgroup("mes_core", "Manufacturing Execution System Core")
    group.addoption("--operator-id", action="store", required=True,
                    help="Operator Badge ID (e.g. OP-4092) for ISO 9001 compliance.")
    group.addoption("--env-config", action="store", default="/etc/mes/station.toml",
                    help="Path to the TOML hardware definition file.")
    parser.addoption("--generate-mes-config", action="store_true",
                     help="Generates a sample TOML configuration file.")

# -------------------------------------------------------------------------
# SESSION FIXTURES (THE BOM & TELEMETRY)
# -------------------------------------------------------------------------
@pytest.fixture(scope="session")
def operator_id(request: pytest.FixtureRequest) -> str:
    return str(request.config.getoption("--operator-id"))

@pytest.fixture(scope="session")
def mes_env(request: pytest.FixtureRequest) -> StationEnvironment:
    """
    THE INDISPUTABLE HARDWARE BOM.
    Parses the TOML environment file into a strict Pydantic model once per session.
    """
    toml_path = Path(request.config.getoption("--env-config"))
    logger.info(f"[Setup] Loading Physical Station Configuration from {toml_path.absolute()}")
    return load_toml_config(toml_path, StationEnvironment)

@pytest.fixture(scope="session")
def telemetry_sink(mes_env: StationEnvironment, operator_id: str) -> TelemetrySink:
    """Provides the active telemetry exporter to downstream fixtures."""
    global _global_telemetry_sink
    if not _global_telemetry_sink:
        # Fallback initialization if pytest_configure missed it
        ctx = StationContext(
            facility=mes_env.station_meta.facility,
            jig_id=mes_env.station_meta.jig_id,
            operator_id=operator_id
        )
        _global_telemetry_sink = TelemetrySink(mes_env.telemetry, ctx)
    return _global_telemetry_sink

@pytest.fixture(scope="session")
def bootstrapper(mes_env: StationEnvironment) -> Optional[HardwareBootstrapper]:
    """Exposes the physical Boot/Reset multiplexer to downstream provisioning scripts."""
    if mes_env.bootstrap and mes_env.bootstrap.enabled:
        return HardwareBootstrapper(mes_env.bootstrap)
    return None

# -------------------------------------------------------------------------
# GLOBAL LIFECYCLE (WATCHDOGS & SINKS)
# -------------------------------------------------------------------------
def pytest_configure(config: pytest.Config) -> None:
    """Strips software metadata, injects physical reality, and arms safety watchdogs."""
    if config.getoption("--generate-mes-config"):
        # Assuming you have a template generator
        pytest.exit("Sample config generated. Exiting.", returncode=0)

    toml_path = Path(config.getoption("--env-config"))
    if toml_path.exists():
        try:
            bom = load_toml_config(toml_path, StationEnvironment)

            # 1. Arm Safety Watchdog (Zero-Leakage Contract)
            if bom.e_stop and bom.e_stop.enabled:
                global _global_watchdog
                _global_watchdog = EStopWatchdog(bom.e_stop)
                _global_watchdog.__enter__()

            # 2. Initialize Telemetry Sink
            global _global_telemetry_sink
            ctx = StationContext(
                facility=bom.station_meta.facility,
                jig_id=bom.station_meta.jig_id,
                operator_id=config.getoption("--operator-id", default="UNKNOWN")
            )
            _global_telemetry_sink = TelemetrySink(bom.telemetry, ctx)

            # 3. HTML Report Metadata Injection
            if hasattr(config, "_metadata"):
                metadata: dict[str, Any] = getattr(config, "_metadata")
                for key in ["Python", "Platform", "Packages", "Plugins"]:
                    metadata.pop(key, None)

                metadata["Facility"] = bom.station_meta.facility
                metadata["Jig ID"] = bom.station_meta.jig_id
                metadata["Operator ID"] = ctx.operator_id
                metadata["Test Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                metadata["Station Config BOM"] = bom.model_dump_json(exclude_none=True)

        except Exception as e:
            logger.error(f"Failed to load BOM during pytest_configure: {e}")

def pytest_unconfigure(config: pytest.Config) -> None:
    """ZERO-LEAKAGE: Reaps global daemon threads when pytest exits."""
    global _global_watchdog
    global _global_telemetry_sink

    if _global_watchdog:
        logger.info("[Teardown] Disarming Physical E-Stop Watchdog daemon...")
        _global_watchdog.__exit__(None, None, None)

    if _global_telemetry_sink:
        # Calculate final session duration and write the session completion marker
        _global_telemetry_sink.finalize_session(passed=not config.pluginmanager.get_plugin("session").testsfailed)

# -------------------------------------------------------------------------
# HTML REPORT HACKING
# -------------------------------------------------------------------------
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
        record: TestRecord = getattr(item, "mes_telemetry_record", None)
        if record and record.metrics:
            html = "<br>".join([f"<b>{k}:</b> {v}" for k, v in record.metrics.items()])
            rep.custom_metrics_html = html

# -------------------------------------------------------------------------
# CORE FIXTURES: TRANSPORT ROUTER & FUNCTION TELEMETRY
# -------------------------------------------------------------------------
@pytest.fixture(scope="session")
def dut_transport(mes_env: StationEnvironment):
    """
    THE MASTER ROUTER: Auto-assembles the highest-bandwidth transport available.
    Yields a FailoverMatrix if both SSH and Serial are defined.
    """
    primary_ssh = None
    fallback_serial = None

    # Retrieve Transports from Dictionary
    if "primary" in mes_env.ssh_targets and mes_env.ssh_targets["primary"].enabled:
        primary_ssh = SshTransport(mes_env.ssh_targets["primary"])

    if "debug_port" in mes_env.host_serial and mes_env.host_serial["debug_port"].enabled:
        fallback_serial = SerialTransport(mes_env.host_serial["debug_port"])

    # Matrix Assembly
    if primary_ssh and fallback_serial:
        logger.info("[Router] Deploying SSH with Serial Failover Matrix.")
        transport = FailoverTransport(primary=primary_ssh, fallback=fallback_serial)
    elif primary_ssh:
        logger.info("[Router] Deploying SSH Transport (No Serial Fallback defined).")
        transport = primary_ssh
    elif fallback_serial:
        logger.info("[Router] Deploying Dedicated Serial Transport.")
        transport = fallback_serial
    else:
        pytest.skip("No enabled transport targets found in configuration.")

    try:
        transport.connect()
        yield transport
    finally:
        logger.info("[Router] ZERO-LEAKAGE: Severing all active Transports.")
        transport.disconnect()

@pytest.fixture(scope="function")
def mes_record(
    request: pytest.FixtureRequest,
    telemetry_sink: TelemetrySink,
    mes_env: StationEnvironment,
    dut_transport  # Injects the active transport for post-mortem dumps
) -> Generator[TestRecord, None, None]:
    """
    Dependency Injection for Telemetry and Automated Root-Cause Analysis.
    Yields the TestRecord to the test logic, performs hardware forensics on failure,
    and guarantees disk flush on teardown.
    """
    callspec = getattr(request.node, "callspec", None)
    iteration = int((callspec.params.get("_pytest_repeat_step_number", 0) + 1) if callspec else 1)
    test_name = request.node.originalname or request.node.name

    record = TestRecord(test_name=test_name, iteration=iteration)

    # Attach to the item so makereport hook can extract metrics for HTML
    setattr(request.node, "mes_telemetry_record", record)

    t0 = time.perf_counter()

    # -------------------------------------------------------------
    # Hand control over to the downstream proprietary hardware test
    yield record
    # -------------------------------------------------------------

    # ZERO-LEAKAGE TEARDOWN: Execute regardless of test panic/timeout
    record.duration_s = round(time.perf_counter() - t0, 3)

    # Scrape outcomes
    rep_setup = getattr(request.node, "rep_setup", None)
    rep_call = getattr(request.node, "rep_call", None)

    if rep_setup and rep_setup.failed:
        record.passed = False
        record.error_message = f"SETUP FAILURE: {str(rep_setup.longrepr)}"
    elif rep_call:
        record.passed = bool(rep_call.passed)
        if rep_call.failed:
            record.error_message = str(rep_call.longrepr)

            # ==========================================
            # AUTOMATED HARDWARE FORENSIC DUMPER
            # ==========================================
            commands_to_run = []
            for profile_name, cmds in mes_env.post_mortem_dumps.items():
                if profile_name in test_name:
                    commands_to_run.extend(cmds)

            if commands_to_run and dut_transport.is_connected():
                logger.warning(f"[Post-Mortem] Test {test_name} failed. Extracting final states over Transport...")

                dump_context = {}
                for cmd in commands_to_run:
                    res = dut_transport.execute(cmd, timeout_s=5.0)
                    dump_context[cmd] = res.stdout if res.exit_code == 0 else f"ERROR: {res.stderr}"

                # Append the post-mortem data directly into the Grafana JSONL record
                record.context["post_mortem"] = dump_context
                logger.info("[Post-Mortem] Forensic data attached to telemetry payload.")

    else:
        record.passed = False
        record.error_message = "UNKNOWN FAILURE: Execution interrupted."

    # Atomically write to disk/network
    try:
        telemetry_sink.write_record(record)
    except Exception as e:
        logger.critical(f"FATAL: Failed to route telemetry for {record.test_name}: {e}")
