# src/pytest_mes_core/plugin.py
import pytest
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Generator, Any, Optional

from pytest_mes_core.config import StationEnvironment, load_toml_config
from pytest_mes_core.host_adapters.safety import EStopWatchdog
from pytest_mes_core.transports import EphemeralSSHClient, EphemeralSerialClient, FailoverTransport
from pytest_mes_core.telemetry.base import StationContext, TestRecord, TelemetryExporter
from pytest_mes_core.telemetry.jsonl_exporter import JsonlTelemetryExporter

logger = logging.getLogger("mes_core.plugin")

_global_watchdog: Optional[EStopWatchdog] = None
_global_telemetry_sink: Optional[TelemetryExporter] = None

def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("mes_core", "Manufacturing Execution System Core")
    group.addoption("--operator-id", action="store", required=True)
    group.addoption("--env-config", action="store", default="station_env.toml")
    parser.addoption("--generate-mes-config", action="store_true")

@pytest.fixture(scope="session")
def operator_id(request: pytest.FixtureRequest) -> str:
    return str(request.config.getoption("--operator-id"))

@pytest.fixture(scope="session")
def mes_env(request: pytest.FixtureRequest) -> StationEnvironment:
    toml_path = Path(request.config.getoption("--env-config"))
    return load_toml_config(toml_path, StationEnvironment)

@pytest.fixture(scope="session")
def telemetry_sink() -> Optional[TelemetryExporter]:
    return _global_telemetry_sink

def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--generate-mes-config"):
        from pytest_mes_core.templates import generate_sample_config
        generate_sample_config()
        pytest.exit("Sample config generated. Exiting.", returncode=0)

    toml_path = Path(config.getoption("--env-config"))
    if toml_path.exists():
        try:
            bom = load_toml_config(toml_path, StationEnvironment)

            if bom.e_stop and bom.e_stop.enabled:
                global _global_watchdog
                _global_watchdog = EStopWatchdog(bom.e_stop)
                _global_watchdog.__enter__()

            global _global_telemetry_sink
            ctx = StationContext(
                facility=bom.station_meta.facility,
                jig_id=bom.station_meta.jig_id,
                operator_id=config.getoption("--operator-id", default="UNKNOWN")
            )

            if bom.telemetry.exporter_type == "jsonl":
                log_dir = Path(bom.telemetry.log_directory) if bom.telemetry.log_directory else Path("artifacts/telemetry")
                _global_telemetry_sink = JsonlTelemetryExporter(log_dir)
                _global_telemetry_sink.start_session(ctx)

            if hasattr(config, "_metadata"):
                metadata: dict[str, Any] = getattr(config, "_metadata")
                for key in ["Python", "Platform", "Packages", "Plugins"]:
                    metadata.pop(key, None)
                metadata["Facility"] = bom.station_meta.facility
                metadata["Jig ID"] = bom.station_meta.jig_id
                metadata["Operator ID"] = ctx.operator_id
                metadata["Test Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            logger.error(f"Failed to load BOM during pytest_configure: {e}")

def pytest_unconfigure(config: pytest.Config) -> None:
    global _global_watchdog, _global_telemetry_sink
    if _global_watchdog:
        _global_watchdog.__exit__(None, None, None)
    if _global_telemetry_sink:
        _global_telemetry_sink.end_session(passed=not config.pluginmanager.get_plugin("session").testsfailed)

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
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)

    if rep.when == "call":
        record: TestRecord = getattr(item, "mes_telemetry_record", None)
        if record and record.result and record.result.metrics:
            html = "<br>".join([f"<b>{k}:</b> {v}" for k, v in record.result.metrics.items()])
            rep.custom_metrics_html = html

@pytest.fixture(scope="session")
def dut_transport(mes_env: StationEnvironment):
    primary_ssh = EphemeralSSHClient(mes_env.ssh_targets["primary"]) if "primary" in mes_env.ssh_targets and mes_env.ssh_targets["primary"].enabled else None
    fallback_serial = EphemeralSerialClient(mes_env.host_serial["debug_port"]) if "debug_port" in mes_env.host_serial and mes_env.host_serial["debug_port"].enabled else None

    if primary_ssh and fallback_serial:
        transport = FailoverTransport(primary=primary_ssh, fallback=fallback_serial)
    elif primary_ssh:
        transport = primary_ssh
    elif fallback_serial:
        transport = fallback_serial
    else:
        pytest.skip("No enabled transport targets found.")

    try:
        transport.connect()
        yield transport
    finally:
        transport.disconnect()

@pytest.fixture(scope="function")
def mes_record(
    request: pytest.FixtureRequest,
    mes_env: StationEnvironment,
    dut_transport  # Injects the active transport for post-mortem dumps
) -> Generator[TestRecord, None, None]:

    callspec = getattr(request.node, "callspec", None)
    iteration = int((callspec.params.get("_pytest_repeat_step_number", 0) + 1) if callspec else 1)
    test_name = request.node.originalname or request.node.name

    global _global_telemetry_sink
    ctx = _global_telemetry_sink.context if _global_telemetry_sink else None
    record = TestRecord(test_name=test_name, iteration=iteration, station_context=ctx)

    setattr(request.node, "mes_telemetry_record", record)

    t0 = time.perf_counter()

    # STRICT ZERO-LEAKAGE TRY/FINALLY CONTRACT
    try:
        # Hand control over to the downstream proprietary hardware test
        yield record
    finally:
        record.duration_s = round(time.perf_counter() - t0, 3)

        # 1. Scrape BOTH Setup and Call outcomes
        rep_setup = getattr(request.node, "rep_setup", None)
        rep_call = getattr(request.node, "rep_call", None)

        if rep_setup and rep_setup.failed:
            record.passed = False
            record.error_message = f"SETUP FAILURE: {str(rep_setup.longrepr)}"
        elif rep_call:
            record.passed = bool(rep_call.passed)
            if rep_call.failed:
                record.error_message = str(rep_call.longrepr)

                # 2. AUTOMATED HARDWARE FORENSIC DUMPER
                commands_to_run = []
                for profile_name, cmds in mes_env.post_mortem_dumps.items():
                    if profile_name in test_name:
                        commands_to_run.extend(cmds)

                if commands_to_run:
                    logger.warning(f"[Post-Mortem] Test {test_name} failed. Extracting final states over Transport...")

                    # Pstore safety net: wait for OS to recover if it kernel panicked
                    if not dut_transport.is_connected:
                        logger.info("[Post-Mortem] Transport dead. Attempting to reconnect...")
                        try:
                            # connect() natively handles timeouts and retries
                            dut_transport.connect()
                        except Exception:
                            logger.error("[Post-Mortem] OS failed to recover. Aborting dumps.")

                    dump_context = {}
                    if dut_transport.is_connected:
                        for cmd in commands_to_run:
                            res = dut_transport.safe_run(cmd, timeout_s=5.0)
                            dump_context[cmd] = res.stdout if res.exited == 0 else f"NO DATA: {res.stderr}"

                    record.context["post_mortem"] = dump_context
                    logger.info("[Post-Mortem] Forensic data attached to telemetry payload.")

        # 3. Flush to Grafana/JSONL
        if _global_telemetry_sink:
            try:
                _global_telemetry_sink.emit_record(record)
            except Exception as e:
                logger.critical(f"FATAL: Failed to route telemetry for {record.test_name}: {e}")
