"""
Telemetry & Reporting Plugin

This module is responsible for capturing the outcome of every test,
formatting it into a standard JSONL schema, and handling fatal hardware
crashes by executing automated forensic post-mortem dumps.
"""

import pytest
import time
import logging
from typing import Generator, Any

from pytest_mes_core.config import StationEnvironment
from pytest_mes_core.telemetry import TestRecord, TelemetryExporter

logger = logging.getLogger("mes_core.telemetry")

# ==========================================
# HTML REPORT MODIFIERS (pytest-html)
# ==========================================

def pytest_html_report_title(report: Any) -> None:
    """Renames the pytest-html report title."""
    report.title = "Manufacturing EOL Certificate"

def pytest_html_results_table_header(cells: list[Any]) -> None:
    """Injects a custom column into the HTML report for physical metrics."""
    cells.insert(2, "<th>Physical Metrics</th>")
    if cells: cells.pop()

def pytest_html_results_table_row(report: Any, cells: list[Any]) -> None:
    """Populates the custom HTML metrics column per test."""
    metrics_html = getattr(report, "custom_metrics_html", "<i>No data</i>")
    cells.insert(2, f"<td>{metrics_html}</td>")
    if cells: cells.pop()

# ==========================================
# PYTEST EXECUTION HOOKS
# ==========================================

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, None, None]:
    """
    Intercepts the test execution phases (setup, call, teardown) to attach
    results and HTML metrics directly to the Pytest report object.
    """
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)

    if rep.when == "call":
        record: TestRecord = getattr(item, "mes_telemetry_record", None)
        if record and record.result and record.result.metrics:
            # Format the dictionary into HTML breaks for the report
            html = "<br>".join([f"<b>{k}:</b> {v}" for k, v in record.result.metrics.items()])
            rep.custom_metrics_html = html

# ==========================================
# CORE TELEMETRY FIXTURE
# ==========================================

@pytest.fixture(scope="function")
def mes_record(
    request: pytest.FixtureRequest,
    mes_env: StationEnvironment,
    telemetry_sink: TelemetryExporter,  # Injected cleanly from core_config!
    dut_transport,                      # Injected from hardware plugin
    dut_state_machine,                  # Injected from orchestrator plugin
    enforce_physical_state              # Force execution order: Boot BEFORE timer starts
) -> Generator[TestRecord, None, None]:
    """
    The Zero-Leakage Telemetry Wrapper.

    This fixture wraps EVERY test in a strict try/finally block. It starts the
    stopwatch, yields to the test, and upon test completion (pass or fail),
    harvests the physical state of the board and emits the JSONL record.
    """

    callspec = getattr(request.node, "callspec", None)
    iteration = int((callspec.params.get("_pytest_repeat_step_number", 0) + 1) if callspec else 1)

    # Use nodeid to capture parameterized variants perfectly (e.g., test_uart[ttymxc3])
    test_node_id = request.node.nodeid

    ctx = telemetry_sink.context if telemetry_sink else None
    record = TestRecord(test_name=test_node_id, iteration=iteration, station_context=ctx)

    # Attach to the Pytest node so `makereport` can find it
    setattr(request.node, "mes_telemetry_record", record)

    t0 = time.perf_counter()

    # STRICT ZERO-LEAKAGE TRY/FINALLY CONTRACT
    try:
        yield record
    finally:
        record.duration_s = round(time.perf_counter() - t0, 3)

        # Harvest Hardware Context from the FSM
        if dut_state_machine:
            record.context["fsm_state"] = dut_state_machine.state.name if hasattr(dut_state_machine.state, 'name') else str(dut_state_machine.state)
            record.context["active_rootfs"] = dut_state_machine.context.active_rootfs
            record.context["boot_medium"] = dut_state_machine.context.active_boot_medium

            if dut_state_machine.context.manifest:
                record.context["manifest"] = dut_state_machine.context.manifest.to_dict()

            record.context.update(dut_state_machine.context.custom_data)

        # Harvest Test Execution Status
        rep_setup = getattr(request.node, "rep_setup", None)
        rep_call = getattr(request.node, "rep_call", None)

        if rep_setup and rep_setup.failed:
            record.passed = False
            short_err = str(rep_setup.longrepr).splitlines()[-1] if rep_setup.longrepr else "Unknown Setup Error"
            record.error_message = f"SETUP FAILURE: {short_err}"

        elif rep_call:
            record.passed = bool(rep_call.passed)
            if rep_call.failed:
                # Clean Error Parsing for dashboards
                full_trace = str(rep_call.longrepr)
                short_err = full_trace.splitlines()[-1] if full_trace else "Unknown Assertion Failure"

                record.error_message = short_err
                record.context["full_traceback"] = full_trace # Stash full trace for deep debugging

                # AUTOMATED HARDWARE FORENSIC DUMPER
                commands_to_run = []
                base_name = request.node.originalname or request.node.name
                for profile_name, cmds in mes_env.post_mortem_dumps.items():
                    if profile_name in base_name:
                        commands_to_run.extend(cmds)

                if commands_to_run:
                    logger.critical("="*60)
                    logger.critical(f"[Post-Mortem] FATAL: Test '{test_node_id}' Failed!")
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
                            res = dut_transport.safe_run(cmd, timeout_s=5.0, check_exit_code=False)
                            dump_context[cmd] = res.stdout if res.exited == 0 else f"NO DATA: {res.stderr}"

                    record.context["post_mortem"] = dump_context
                    logger.info("[Post-Mortem] Forensic data successfully attached to telemetry payload.")

        # Emit to JSONL
        if telemetry_sink:
            try:
                telemetry_sink.emit_record(record)
            except Exception as e:
                logger.critical("="*60)
                logger.critical(f"[Telemetry] FATAL: FAILED TO ROUTE TELEMETRY FOR {record.test_name}!")
                logger.critical(f"[Telemetry] Exception: {e}")
                logger.critical("="*60)
