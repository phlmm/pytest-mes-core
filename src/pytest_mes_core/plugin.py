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
from pytest_mes_core.telemetry import StationContext, TestRecord
from pytest_mes_core.telemetry import JsonlTelemetryExporter, TelemetryExporter
from pytest_mes_core.transports import TransportConnectionError, TransportTimeoutError

# --- NEW: State Machine & Instruments ---
from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine, DutState
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply

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
    """Dynamically maps Pytest's -v and -vv flags to live log streaming levels."""

    # Register the FSM marker
    config.addinivalue_line(
        "markers", "requires_state(state): Enforces physical hardware state (DutState) before test execution."
    )

    config.option.log_cli = True

    # Define the output format (Beautiful, aligned, and timestamped)
    config.option.log_cli_format = "%(asctime)s [%(levelname)7s] %(name)s: %(message)s"
    config.option.log_cli_date_format = "%H:%M:%S"

    verbosity = config.getoption("verbose")

    if verbosity == 0:
        # 'pytest': Total Silence. Only operator actions and hardware failures.
        config.option.log_cli_level = "WARNING"
    elif verbosity == 1:
        # 'pytest -v': High-Level Progress (Milestones, Tool executions)
        config.option.log_cli_level = "INFO"
    else:
        # 'pytest -vv': The Matrix (Raw UART bytes, SSH traces, Hex dumps)
        config.option.log_cli_level = "DEBUG"

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

            logger.info(f"[Framework] Bootstrapping MES Session for Jig: {bom.station_meta.jig_id}")

        except Exception as e:
            logger.critical(f"[Framework] FATAL: Failed to load Hardware BOM during pytest_configure: {e}")

def pytest_unconfigure(config: pytest.Config) -> None:
    global _global_watchdog, _global_telemetry_sink
    if _global_watchdog:
        _global_watchdog.__exit__(None, None, None)
    if _global_telemetry_sink:
        _global_telemetry_sink.end_session(session_passed=not bool(config.pluginmanager.get_plugin("session").testsfailed))

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

# ==========================================
# HARDWARE FIXTURES
# ==========================================

@pytest.fixture(scope="session")
def psu_hardware(mes_env: StationEnvironment) -> Generator[Optional[ScpiPowerSupply], None, None]:
    if not mes_env.psu_hardware or not mes_env.psu_hardware.enabled:
        yield None   # afely yield None to Pytest
        return       # Then exit the generator

    psu = ScpiPowerSupply(mes_env.psu_hardware)
    psu.connect()
    yield psu
    psu.close()

@pytest.fixture(scope="session")
def ssh_client(mes_env: StationEnvironment) -> Optional[EphemeralSSHClient]:
    if "primary" in mes_env.ssh_targets and mes_env.ssh_targets["primary"].enabled:
        return EphemeralSSHClient(mes_env.ssh_targets["primary"])
    return None

@pytest.fixture(scope="session")
def serial_client(mes_env: StationEnvironment) -> Optional[EphemeralSerialClient]:
    if "debug_port" in mes_env.host_serial and mes_env.host_serial["debug_port"].enabled:
        return EphemeralSerialClient(mes_env.host_serial["debug_port"])
    return None

@pytest.fixture(scope="session")
def dut_transport(mes_env, ssh_client, serial_client): # <-- ADD mes_env here
    if ssh_client and serial_client:
        transport = FailoverTransport(primary=ssh_client, fallback=serial_client)
    elif ssh_client:
        transport = ssh_client
    elif serial_client:
        transport = serial_client
    else:
        pytest.skip("No enabled transport targets found.")
        return

    # THE FIX: Prevent Split-Brain!
    # If the FSM is active, IT owns the connection timing. We do not connect here.
    # If the FSM is disabled, we fallback to the legacy "blind connect" behavior.
    fsm_active = hasattr(mes_env, "state_machine") and mes_env.state_machine and mes_env.state_machine.enabled

    if not fsm_active:
        try:
            transport.connect()
            logger.info("[Fixture] DUT Transport Matrix connected successfully.")
        except (TransportConnectionError, TransportTimeoutError) as e:
            logger.warning(f"[Fixture] DUT Transport offline during setup. Reason: {e}")
        except Exception as e:
            logger.error(f"[Fixture] Unexpected transport failure: {e}")
    else:
        logger.info("[Fixture] FSM is active. Deferring Transport socket binding to State Machine.")

    try:
        yield transport
    finally:
        transport.disconnect()

# ==========================================
# STATE MACHINE ORCHESTRATION
# ==========================================

@pytest.fixture(scope="session")
def dut_state_machine(mes_env, psu_hardware, serial_client, ssh_client):
    """The master session state machine. Owns physical execution state."""
    if not hasattr(mes_env, "state_machine") or not mes_env.state_machine or not mes_env.state_machine.enabled:
        yield None
        return

    # THE FIX: We removed `psu_hardware` from this strict check.
    # Serial and SSH are still mandatory to track OS states!
    if not serial_client or not ssh_client:
        logger.warning("[State Machine] Missing required UART or SSH transports. State machine disabled.")
        yield None
        return

    boot_profiler_cfg = mes_env.boot_profilers.get("linux_boot") if mes_env.boot_profilers else None

    sm = EmbeddedLinuxStateMachine(
        psu=psu_hardware, # Now safely passes None if the PSU is missing in TOML!
        serial=serial_client,
        ssh=ssh_client,
        cfg=mes_env.state_machine,
        boot_profiler_cfg=boot_profiler_cfg
    )

    yield sm

    if sm.boot_metrics:
        logger.info(f"[Metrics] Final Boot Performance: {sm.boot_metrics}")

    sm.power_off()

@pytest.fixture(autouse=True)
def enforce_physical_state(request: pytest.FixtureRequest, dut_state_machine: Optional[EmbeddedLinuxStateMachine]):
    """
    Runs automatically before EVERY test.
    Reads the marker and enforces the physical state.
    """
    if not dut_state_machine:
        yield # Fallback: Run standard test flow if FSM is disabled
        return

    marker = request.node.get_closest_marker("requires_state")
    target_state = marker.args[0].name if marker else 'OS_USERLAND'

    # State Resolution via 'transitions' library
    if dut_state_machine.state == target_state:
        pass # Already there
    elif target_state == 'POWER_OFF':
        dut_state_machine.power_off()
    elif target_state == 'ENERGIZED':
        dut_state_machine.energize()
    elif target_state == 'BOOTLOADER':
        dut_state_machine.boot_to_bootloader()
    elif target_state == 'OS_USERLAND':
        dut_state_machine.boot_to_os()

    yield # TEST EXECUTES HERE

    # Forensic Check: Mark DIRTY if test asserts
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call and rep_call.failed:
        dut_state_machine.mark_dirty(reason=f"Test '{request.node.name}' failed.")

# ==========================================
# TELEMETRY & POST-MORTEM
# ==========================================

@pytest.fixture(scope="function")
def mes_record(
    request: pytest.FixtureRequest,
    mes_env: StationEnvironment,
    dut_transport,
    enforce_physical_state # Force execution order: Boot the board BEFORE starting the timer!
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
        yield record
    finally:
        record.duration_s = round(time.perf_counter() - t0, 3)

        rep_setup = getattr(request.node, "rep_setup", None)
        rep_call = getattr(request.node, "rep_call", None)

        if rep_setup and rep_setup.failed:
            record.passed = False
            record.error_message = f"SETUP FAILURE: {str(rep_setup.longrepr)}"
        elif rep_call:
            record.passed = bool(rep_call.passed)
            if rep_call.failed:
                record.error_message = str(rep_call.longrepr)

                # AUTOMATED HARDWARE FORENSIC DUMPER
                commands_to_run = []
                for profile_name, cmds in mes_env.post_mortem_dumps.items():
                    if profile_name in test_name:
                        commands_to_run.extend(cmds)

                if commands_to_run:
                    logger.critical("="*60)
                    logger.critical(f"[Post-Mortem] FATAL: Test '{test_name}' Failed!")
                    logger.critical("[Post-Mortem] Engaging automated hardware forensic dumper...")
                    logger.critical("="*60)

                    if not dut_transport.is_connected:
                        logger.warning("[Post-Mortem] Transport dead. Attempting recovery to scrape crash logs...")
                        try:
                            dut_transport.connect()
                        except Exception:
                            logger.error("[Post-Mortem] OS failed to recover. Aborting forensic dumps.")

                    dump_context = {}
                    if dut_transport.is_connected:
                        for cmd in commands_to_run:
                            res = dut_transport.safe_run(cmd, timeout_s=5.0)
                            dump_context[cmd] = res.stdout if res.exited == 0 else f"NO DATA: {res.stderr}"

                    record.context["post_mortem"] = dump_context
                    logger.info("[Post-Mortem] Forensic data successfully attached to telemetry payload.")

        if _global_telemetry_sink:
            try:
                _global_telemetry_sink.emit_record(record)
            except Exception as e:
                logger.critical("="*60)
                logger.critical(f"[Telemetry] FATAL: FAILED TO ROUTE TELEMETRY FOR {record.test_name}!")
                logger.critical(f"[Telemetry] Exception: {e}")
                logger.critical("="*60)
