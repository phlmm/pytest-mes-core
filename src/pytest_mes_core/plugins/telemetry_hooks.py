import structlog
"""
Telemetry & Reporting Plugin

This module is responsible for capturing the outcome of every test,
formatting it into a standard JSONL schema, and handling fatal hardware
crashes by executing automated forensic post-mortem dumps.
"""
import pytest
import time
import logging
from typing import Generator, Any, Optional
from pytest_mes_core.config import StationEnvironment
from pytest_mes_core.telemetry import TestRecord, TelemetryExporter
from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine
logger = structlog.get_logger('mes_core.telemetry')

def pytest_html_report_title(report: Any) -> None:
    """Renames the pytest-html report title.

    Args:
        report: The Pytest HTML report object.
    """
    report.title = 'Manufacturing EOL Certificate'

def pytest_html_results_table_header(cells: list[Any]) -> None:
    """Injects a custom column into the HTML report for physical metrics.

    Args:
        cells: The list of table header cells.
    """
    cells.insert(2, '<th>Physical Metrics</th>')
    if cells:
        cells.pop()

def pytest_html_results_table_row(report: Any, cells: list[Any]) -> None:
    """Populates the custom HTML metrics column per test.

    Args:
        report: The Pytest HTML report object.
        cells: The list of table row cells.
    """
    metrics_html = getattr(report, 'custom_metrics_html', '<i>No data</i>')
    cells.insert(2, f'<td>{metrics_html}</td>')
    if cells:
        cells.pop()

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, Any, None]:
    """
    Intercepts the test execution phases (setup, call, teardown) to attach
    results and HTML metrics directly to the Pytest report object.

    Args:
        item: The Pytest item object.
        call: The Pytest call info object.

    Yields:
        None
    """
    outcome: Any = (yield)
    rep = outcome.get_result()
    setattr(item, 'rep_' + rep.when, rep)
    if rep.when == 'call':
        record: Optional[TestRecord] = getattr(item, 'mes_telemetry_record', None)
        if record:
            html_parts = []
            if 'active_rootfs' in record.context:
                html_parts.append(f"<span style='color: gray'>RootFS: {record.context['active_rootfs']}</span>")
            if 'boot_medium' in record.context:
                html_parts.append(f"<span style='color: gray'>Boot: {record.context['boot_medium']}</span>")

            # Merge metrics from both sources:
            #   record.metrics  — direct dict populated by any test (e.g. calib_* from test_08)
            #   record.result.metrics — from ValidatorResult.absorb() (e.g. hardware validators)
            # record.metrics takes precedence on key conflicts.
            merged_metrics: dict = {}
            if record.result and record.result.metrics:
                merged_metrics.update(record.result.metrics)
            if record.metrics:
                merged_metrics.update(record.metrics)

            if merged_metrics:
                if html_parts:
                    html_parts.append("<hr style='margin: 4px 0; border: 0; border-top: 1px solid #ccc;'>")
                # Split calib_ keys into their own block for visual separation
                hw_metrics = {k: v for k, v in merged_metrics.items() if not k.startswith('calib_')}
                calib_metrics = {k: v for k, v in merged_metrics.items() if k.startswith('calib_')}
                if hw_metrics:
                    html_parts.append('<br>'.join([f'<b>{k}:</b> {v}' for k, v in hw_metrics.items()]))
                if calib_metrics:
                    if hw_metrics:
                        html_parts.append("<hr style='margin: 4px 0; border: 0; border-top: 1px dashed #aaa;'>")
                    html_parts.append("<span style='color:#555;font-size:0.9em'><b>Calibration</b></span>")
                    html_parts.append('<br>'.join([
                        f"<span style='color:#555'><b>{k[6:]}:</b> {v}</span>"  # strip 'calib_' prefix for display
                        for k, v in calib_metrics.items()
                    ]))

            if html_parts:
                rep.custom_metrics_html = '<br>'.join(html_parts)

@pytest.fixture(scope='function', autouse=True)
def mes_record(request: pytest.FixtureRequest, mes_env: StationEnvironment, telemetry_sink: TelemetryExporter, dut_transport: Any, dut_state_machine: Optional[EmbeddedLinuxStateMachine], enforce_physical_state: Any) -> Generator[TestRecord, None, None]:
    """
    The Zero-Leakage Telemetry Wrapper.

    This fixture wraps EVERY test in a strict try/finally block. It starts the
    stopwatch, yields to the test, and upon test completion (pass or fail),
    harvests the physical state of the board and emits the JSONL record.
    """
    test_node_id = request.node.nodeid
    callspec = getattr(request.node, 'callspec', None)
    loop_iteration = int(callspec.params.get('_pytest_repeat_step_number', 0) + 1 if callspec else 1)
    execution_count = getattr(request.node, 'execution_count', 1)
    ctx = telemetry_sink.context if telemetry_sink else None
    record = TestRecord(test_name=test_node_id, iteration=execution_count, station_context=ctx)
    record.context['is_retry'] = execution_count > 1
    record.context['stress_loop_iteration'] = loop_iteration
    setattr(request.node, 'mes_telemetry_record', record)
    if execution_count > 1:
        logger.warning('=' * 60)
        logger.warning('executing_hardware_retry_attempt_execution_count', execution_count=execution_count)
        logger.warning('=' * 60)
    t0 = time.perf_counter()
    is_async = request.node.get_closest_marker('anyio') is not None
    
    if dut_transport and getattr(dut_transport, 'is_connected', False):
        if not is_async:
            try:
                dut_transport.safe_run('dmesg -c >/dev/null 2>&1', timeout_s=2.0, check_exit_code=False)
            except Exception:
                pass
        else:
            logger.debug('[Telemetry] Skipping sync dmesg flush; test is async.')
    try:
        yield record
    finally:
        record.duration_s = round(time.perf_counter() - t0, 3)
        if dut_state_machine:
            record.context['fsm_state'] = dut_state_machine.state.name if hasattr(dut_state_machine.state, 'name') else str(dut_state_machine.state)
            record.context['active_rootfs'] = dut_state_machine.context.active_rootfs
            record.context['boot_medium'] = dut_state_machine.context.active_boot_medium
            if dut_state_machine.context.manifest:
                record.context['manifest'] = dut_state_machine.context.manifest.to_dict()
            record.context.update(dut_state_machine.context.custom_data)
        rep_setup = getattr(request.node, 'rep_setup', None)
        rep_call = getattr(request.node, 'rep_call', None)
        if rep_setup and rep_setup.failed:
            record.passed = False
            short_err = str(rep_setup.longrepr).splitlines()[-1] if rep_setup.longrepr else 'Unknown Setup Error'
            record.error_message = f'SETUP FAILURE: {short_err}'
        elif rep_call:
            record.passed = bool(rep_call.passed)
            if rep_call.failed:
                full_trace = str(rep_call.longrepr)
                short_err = full_trace.splitlines()[-1] if full_trace else 'Unknown Assertion Failure'
                record.error_message = short_err
                record.context['full_traceback'] = full_trace
                commands_to_run = []
                base_name = request.node.originalname or request.node.name
                for profile_name, cmds in mes_env.post_mortem_dumps.items():
                    if profile_name in base_name:
                        commands_to_run.extend(cmds)
                if commands_to_run:
                    logger.critical('=' * 60)
                    logger.critical('fatal_test_test_node_id_failed', test_node_id=test_node_id)
                    logger.critical('[Post-Mortem] Engaging automated hardware forensic dumper...')
                    logger.critical('=' * 60)
                    if dut_transport:
                        if not dut_transport.is_connected:
                            logger.warning('[Post-Mortem] Transport dead. Attempting recovery to scrape crash logs...')
                            try:
                                dut_transport.connect()
                            except Exception:
                                logger.error('[Post-Mortem] OS failed to recover. Aborting forensic dumps.')
                        dump_context = {}
                        if dut_transport.is_connected:
                            # Always use the sync path for post-mortem forensics.
                            # Teardown runs *after* pytest-anyio has torn down the event loop,
                            # so calling anyio.run() here would start a *second* loop and
                            # crash with RuntimeError: "This event loop is already running".
                            # safe_run() routes via SSH (primary) or UART (fallback) — either
                            # transport supports the sync variant.
                            for cmd in commands_to_run:
                                try:
                                    res = dut_transport.safe_run(cmd, timeout_s=5.0, check_exit_code=False)
                                    dump_context[cmd] = res.stdout if res.exited == 0 else f'NO DATA: {res.stderr}'
                                except Exception as dump_exc:
                                    dump_context[cmd] = f'DUMP_FAILED: {dump_exc}'
                        record.context['post_mortem'] = dump_context
                        logger.info('[Post-Mortem] Forensic data successfully attached to telemetry payload.')
        if telemetry_sink:
            try:
                telemetry_sink.emit_record(record)
            except Exception as e:
                logger.critical('=' * 60)
                logger.critical('fatal_failed_to_route_telemetry_for_test_name', test_name=record.test_name)
                logger.critical('exception_e', e=e)
                logger.critical('=' * 60)
                pytest.fail(f'CRITICAL MES FAILURE: Telemetry payload was dropped! The board may have passed, but the data did not reach the disk. Reason: {e}', pytrace=False)