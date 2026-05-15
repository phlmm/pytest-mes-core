import structlog
"""
Core Configuration & Session Lifecycle Plugin

This module acts as the initialization layer for the pytest-mes-core framework.
It manages command-line argument parsing, hardware configuration validation (via TOML),
and the global execution lifecycle, including safety systems and telemetry.
"""
import os
import pytest
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from pytest_mes_core.config import StationEnvironment, load_toml_config
from pytest_mes_core.host_adapters.safety import EStopWatchdog
from pytest_mes_core.host_adapters.diagnostics import ResourceDiagnostics
from pytest_mes_core.telemetry import StationContext, TelemetryExporter, JsonlTelemetryExporter, OperatorReceiptExporter, DeveloperMarkdownExporter, CompositeTelemetryExporter
logger = structlog.get_logger('mes_core.config')

def pytest_addoption(parser: pytest.Parser) -> None:
    """
    Registers custom command-line arguments for the MES framework.

    Args:
        parser (pytest.Parser): The Pytest CLI argument parser.
    """
    group = parser.getgroup('mes_core', 'Manufacturing Execution System Core')
    group.addoption('--operator-id', action='store', required=True, help='The ID of the technician or CI pipeline running the test (Required for traceability).')
    group.addoption('--board-serial', action='store', default='UNKNOWN', help='The Serial Number of the DUT for lifecycle traceability.')
    group.addoption('--work-order', action='store', default='UNKNOWN', help='The Manufacturing Work Order string.')
    group.addoption('--env-config', action='store', default='station_env.toml', help='Path to the TOML hardware configuration file.')
    group.addoption('--generate-mes-config', action='store_true', help='Generates a default TOML template and exits.')
    group.addoption('--hold-on-fail', action='store_true', help='Ergonomic debugging flag. Pauses execution and keeps hardware powered on if a test fails.')
    group.addoption('--mock-hardware', action='store_true', default=False, help='Bypasses physical transports. Injects a Mock Transport for CI/CD pipeline testing.')
    group.addoption('--calibration-git-token', action='store', default=None, help='Git Bearer Token for cloning the calibration values repository.')
    group.addoption('--calibration-git-user', action='store', default=None, help='Git Username for Basic Authentication (paired with token/password).')
    group.addoption('--calibration-git-ignore-ssl', action='store_true', default=False, help='Disable SSL certificate verification when cloning the calibration repo.')
    group.addoption('--calibration-client-cert', action='store', default=None, help='Path to the client certificate for Git mutual TLS.')
    group.addoption('--calibration-client-key', action='store', default=None, help='Path to the client private key for Git mutual TLS.')

def pytest_load_initial_conftests(early_config: pytest.Config, parser: pytest.Parser, args: list[str]) -> None:
    """
    The Pre-Parse Hook.
    Intercepts the raw CLI arguments and injects the pytest-html flags BEFORE
    the plugin manager initializes, forcing pytest-html to wake up.

    Args:
        early_config: The early pytest configuration object.
        parser: The argument parser.
        args: The raw list of command-line arguments.
    """
    if not any((arg.startswith('--html') for arg in args)):
        args.extend(['--html=.mes_tmp_report.html', '--self-contained-html'])

@pytest.fixture(scope='session')
def operator_id(request: pytest.FixtureRequest) -> str:
    """
    Retrieves the operator identifier passed via the command-line interface.
    """
    return str(request.config.getoption('--operator-id'))

@pytest.fixture(scope='session')
def mes_env(request: pytest.FixtureRequest) -> StationEnvironment:
    """
    Parses the hardware TOML configuration into a strongly typed Python object.
    """
    toml_path = Path(request.config.getoption('--env-config'))
    return load_toml_config(toml_path, StationEnvironment)

@pytest.fixture(scope='session')
def telemetry_sink(request: pytest.FixtureRequest) -> Optional[TelemetryExporter]:
    """
    Provides direct access to the active telemetry pipeline exporter.
    """
    return getattr(request.config, '_mes_telemetry_sink', None)

def pytest_configure(config: pytest.Config) -> None:
    """
    The Master Setup Hook. Executes once before any test collection begins.

    Args:
        config: The Pytest configuration object.
    """
    config.addinivalue_line('markers', 'requires_state(state): Enforces physical hardware state (DutState) before test execution.')
    config.addinivalue_line('markers', 'hardware_retry(retries): If a test fails, marks hardware DIRTY, forces a cold-boot, and retries.')
    config.option.log_cli = True
    config.option.log_cli_format = '%(asctime)s [%(levelname)-8s] %(name)s: %(message)s'
    config.option.log_cli_date_format = '%H:%M:%S'

    verbosity = config.getoption('verbose')
    if verbosity == 0:
        _level = logging.WARNING
        _level_name = 'WARNING'
    elif verbosity == 1:
        _level = logging.INFO
        _level_name = 'INFO'
    else:
        _level = logging.DEBUG
        _level_name = 'DEBUG'

    # ── stdlib logging ────────────────────────────────────────────────────────
    config.option.log_cli_level = _level_name
    logging.getLogger('transitions').setLevel(_level)
    logging.getLogger('paramiko').setLevel(_level)

    # ── structlog → stdlib bridge ─────────────────────────────────────────────
    # Route every structlog call through the Python stdlib logging tree so that
    # pytest's log_cli_level, caplog, and --log-level all apply uniformly.
    #
    # Design: we use a plain string-rendering terminal processor instead of the
    # wrap_for_formatter + ProcessorFormatter pattern.  ProcessorFormatter only
    # works when YOU control every handler's formatter — pytest installs its own
    # LogCaptureHandler AFTER pytest_configure returns, bypassing any formatter
    # we set on the root logger.  A string renderer avoids this entirely:
    # structlog converts the event dict to a string before handing off to stdlib,
    # so pytest's %(message)s gets clean text regardless of which handler fires.
    import structlog as _sl

    # Keys that pytest's log_cli_format already provides — strip from inline output.
    _BOILERPLATE = frozenset({"level", "logger", "timestamp"})

    def _mes_log_renderer(logger_name, method, event_dict):
        """Terminal structlog processor → clean stdlib-compatible string.

        Output format:  <event>  [key=val ...]
        Example:        tezi_flash_done  duration_s=3.93 target='eMMC'
        """
        msg = str(event_dict.pop("event", ""))
        extras = "  ".join(
            f"{k}={v}" for k, v in event_dict.items()
            if k not in _BOILERPLATE
        )
        return f"{msg}  {extras}" if extras else msg

    _sl.configure(
        processors=[
            _sl.contextvars.merge_contextvars,
            _sl.processors.StackInfoRenderer(),
            _sl.processors.format_exc_info,
            _mes_log_renderer,          # terminal: returns a plain string
        ],
        logger_factory=_sl.stdlib.LoggerFactory(),
        wrapper_class=_sl.make_filtering_bound_logger(_level),
        cache_logger_on_first_use=True,
    )

    # Align root logger level so stdlib propagation doesn't silently drop records.
    logging.getLogger().setLevel(_level)
    toml_path = Path(config.getoption('--env-config'))
    if toml_path.exists():
        try:
            bom = load_toml_config(toml_path, StationEnvironment)
            config._mes_bom = bom
            if bom.e_stop and bom.e_stop.enabled:
                watchdog = EStopWatchdog(bom.e_stop)
                try:
                    watchdog.__enter__()
                    config._mes_watchdog = watchdog
                except Exception as e:
                    logger.critical('fatal_e_stop_watchdog_failed_to_arm_e', e=e)
                    try:
                        watchdog.__exit__(None, None, None)
                    except Exception:
                        pass
            session_id = str(uuid.uuid4())
            ctx = StationContext(facility=bom.station_meta.facility, jig_id=bom.station_meta.jig_id, operator_id=config.getoption('--operator-id', default='UNKNOWN'), dut_serial=config.getoption('--board-serial', default='UNKNOWN'), work_order=config.getoption('--work-order', default='UNKNOWN'))
            setattr(ctx, 'run_id', session_id)
            if bom.telemetry.exporter_type == 'jsonl':
                target_dir = Path(bom.telemetry.log_directory) if bom.telemetry.log_directory else Path('artifacts/evse_telemetry')
                config._mes_telemetry_target_dir = target_dir
                log_dir = Path('/tmp/mes_telemetry_spool') / session_id
                config._mes_telemetry_spool_dir = log_dir
                active_exporters = [JsonlTelemetryExporter(log_dir)]
                if bom.station_meta.environment in ['lab', 'developer']:
                    active_exporters.append(DeveloperMarkdownExporter(log_dir))
                else:
                    active_exporters.append(OperatorReceiptExporter(log_dir))
                telemetry_sink = CompositeTelemetryExporter(active_exporters)
                try:
                    telemetry_sink.start_session(ctx)
                    config._mes_telemetry_sink = telemetry_sink
                except Exception as e:
                    logger.critical('fatal_telemetry_sub_system_failed_to_initialize_e', e=e)
                    pytest.exit(f'MES Framework aborted. Cannot guarantee telemetry storage: {e}', returncode=1)
                date_str = datetime.now().strftime('%Y-%m-%d')
                html_dir = log_dir / 'html_reports' / date_str
                html_dir.mkdir(parents=True, exist_ok=True)
                config._mes_html_dir = html_dir
            if hasattr(config, '_metadata'):
                metadata: dict[str, Any] = getattr(config, '_metadata')
                for key in ['Python', 'Platform', 'Packages', 'Plugins']:
                    metadata.pop(key, None)
                metadata['Facility'] = bom.station_meta.facility
                metadata['Jig ID'] = bom.station_meta.jig_id
                metadata['Operator ID'] = ctx.operator_id
                metadata['Run UUID'] = session_id
                metadata['Board Serial'] = ctx.dut_serial
                metadata['Work Order'] = ctx.work_order
                metadata['Test Timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            logger.info('bootstrapping_mes_session_for_jig_jig_id', jig_id=bom.station_meta.jig_id)
        except Exception as e:
            logger.critical('fatal_failed_to_load_hardware_bom_e', e=e)

def pytest_sessionstart(session: pytest.Session) -> None:
    """
    Pre-flight resource check — runs once after collection, before any test setup.

    Iterates over every serial port declared in the TOML BOM (host_serial entries
    and the PSU serial port when vendor-specific serial comms are used) and verifies
    that no competing process holds a file-lock on the device node.

    If a lock is detected the session is aborted immediately with a clear operator
    message, avoiding the harder-to-diagnose 'device or resource busy' crash that
    would otherwise surface mid-test during EphemeralSerialClient.connect().
    """
    config = session.config
    if config.getoption('--mock-hardware', default=False):
        return

    bom: Optional[StationEnvironment] = getattr(config, '_mes_bom', None)
    if bom is None:
        return  # TOML not loaded (e.g. --generate-mes-config run), skip silently.

    # Collect every serial port path that the framework intends to open.
    ports_to_check: dict[str, str] = {}  # alias -> /dev/path

    for alias, serial_cfg in bom.host_serial.items():
        if serial_cfg.enabled:
            ports_to_check[alias] = serial_cfg.port

    # PSU hardware that uses a serial port (e.g. FNIRSI DPS150 on /dev/ttyUSBx)
    if bom.psu_hardware and bom.psu_hardware.enabled:
        psu_port = getattr(bom.psu_hardware, 'serial_port', None)
        if psu_port:
            ports_to_check['psu_hardware'] = psu_port

    if not ports_to_check:
        return

    logger.info('pre_flight_resource_check_scanning_serial_ports', count=len(ports_to_check))
    locked: list[str] = []
    for alias, port in ports_to_check.items():
        owner = ResourceDiagnostics.get_device_owner(port)
        if owner:
            locked.append(f"  [{alias}] {port}  →  locked by {owner}")
            logger.critical(
                'pre_flight_resource_locked',
                alias=alias, port=port, owner=owner,
            )

    if locked:
        lines = '\n'.join(locked)
        pytest.exit(
            f"\n{'=' * 64}\n"
            f"[PRE-FLIGHT FAIL] Serial port(s) locked by another process:\n"
            f"{lines}\n"
            f"Close minicom / screen / picocom and retry.\n"
            f"{'=' * 64}",
            returncode=3,
        )


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """
    Runs just before pytest-html generates the report.
    Updates the environment table with the fully populated hardware manifest.

    Args:
        session: The Pytest session object.
        exitstatus: The exit status code.
    """
    config = session.config
    telemetry_sink = getattr(config, '_mes_telemetry_sink', None)
    if not telemetry_sink or not telemetry_sink.context:
        return
    ctx = telemetry_sink.context
    if hasattr(config, '_metadata'):
        metadata = getattr(config, '_metadata')
        som_sn = ctx.dut_manifest.get('serial_number', ctx.dut_serial)
        board_sn = ctx.dut_manifest.get('evse_carrier_serial', 'UNKNOWN')
        hw_sn = ctx.dut_manifest.get('HW_SN_CARRIER', '')
        metadata['Board Serial'] = board_sn
        metadata['SOM Serial'] = som_sn
        if hw_sn:
            metadata['HW SN'] = str(hw_sn)
        for k, v in ctx.dut_manifest.items():
            if k not in ['serial_number', 'evse_carrier_serial', 'HW_SN_CARRIER', 'custom_flags'] and v:
                pretty_key = k.replace('_', ' ').title()
                metadata[pretty_key] = str(v)

def pytest_html_results_summary(prefix: list[str], summary: list[str], postfix: list[str], session: pytest.Session) -> None:
    """
    Injects the Hardware Manifest directly into the Summary section of the pytest-html report.
    This guarantees it is visually front-and-center, bypassing any Environment table limitations.

    Args:
        prefix: The prefix elements for the HTML summary.
        summary: The core summary elements.
        postfix: The postfix elements for the HTML summary.
        session: The Pytest session object.
    """
    telemetry_sink = getattr(session.config, '_mes_telemetry_sink', None)
    if not telemetry_sink or not telemetry_sink.context:
        return
    import html
    ctx = telemetry_sink.context
    html_block = '<h2>Hardware Manifest (Station BOM)</h2>'
    html_block += "<table style='width: 100%; border-collapse: collapse; margin-bottom: 20px; font-family: monospace;'>"
    html_block += "<tr style='background-color: #f2f2f2;'><th style='border: 1px solid #ddd; padding: 8px; text-align: left;'>Component</th><th style='border: 1px solid #ddd; padding: 8px; text-align: left;'>Identifier</th></tr>"
    som_sn = ctx.dut_manifest.get('serial_number', ctx.dut_serial)
    board_sn = ctx.dut_manifest.get('evse_carrier_serial', 'UNKNOWN')
    hw_sn = ctx.dut_manifest.get('HW_SN_CARRIER', '')
    html_block += f"<tr><td style='border: 1px solid #ddd; padding: 8px;'>Board Serial</td><td style='border: 1px solid #ddd; padding: 8px;'><b>{html.escape(str(board_sn))}</b></td></tr>"
    html_block += f"<tr><td style='border: 1px solid #ddd; padding: 8px;'>SOM Serial</td><td style='border: 1px solid #ddd; padding: 8px;'><b>{html.escape(str(som_sn))}</b></td></tr>"
    if hw_sn:
        html_block += f"<tr><td style='border: 1px solid #ddd; padding: 8px;'>HW SN</td><td style='border: 1px solid #ddd; padding: 8px;'><b>{html.escape(str(hw_sn))}</b></td></tr>"
    for k, v in ctx.dut_manifest.items():
        if k not in ['serial_number', 'evse_carrier_serial', 'HW_SN_CARRIER', 'custom_flags'] and v:
            pretty_key = k.replace('_', ' ').title()
            html_block += f"<tr><td style='border: 1px solid #ddd; padding: 8px;'>{html.escape(pretty_key)}</td><td style='border: 1px solid #ddd; padding: 8px;'><b>{html.escape(str(v))}</b></td></tr>"
    html_block += '</table>'
    if getattr(ctx, 'software_manifest', None):
        for component_name, component_data in ctx.software_manifest.items():
            if not isinstance(component_data, dict):
                component_data = {component_name: component_data}
                component_name = 'System'
            html_block += f'<h2>Software Build Version: {html.escape(component_name)}</h2>'
            html_block += "<table style='width: 100%; border-collapse: collapse; margin-bottom: 20px; font-family: monospace;'>"
            html_block += "<tr style='background-color: #e6f7ff;'><th style='border: 1px solid #ddd; padding: 8px; text-align: left;'>Key</th><th style='border: 1px solid #ddd; padding: 8px; text-align: left;'>Value</th></tr>"
            for k, v in component_data.items():
                if v:
                    html_block += f"<tr><td style='border: 1px solid #ddd; padding: 8px;'>{html.escape(k)}</td><td style='border: 1px solid #ddd; padding: 8px;'><b>{html.escape(str(v))}</b></td></tr>"
            html_block += '</table>'
    prefix.extend([html_block])

def pytest_unconfigure(config: pytest.Config) -> None:
    """
    The Master Teardown Hook. Executes unconditionally after all tests finish
    or if the framework crashes fatally.

    Args:
        config: The Pytest configuration object.
    """
    watchdog = getattr(config, '_mes_watchdog', None)
    telemetry_sink = getattr(config, '_mes_telemetry_sink', None)
    if watchdog:
        watchdog.__exit__(None, None, None)
    if telemetry_sink:
        tests_failed = bool(config.pluginmanager.get_plugin('session').testsfailed)
        session_passed = not tests_failed
        telemetry_sink.end_session(session_passed=session_passed)
        htmlpath = getattr(config.option, 'htmlpath', None)
        if htmlpath and os.path.exists(htmlpath):
            try:
                ctx = telemetry_sink.context if telemetry_sink else None
                status = 'PASS' if session_passed else 'FAIL'
                time_str = datetime.now().strftime('%H-%M-%S')
                safe_operator = ctx.operator_id.replace('/', '_') if ctx else 'UNKNOWN'
                serial = ctx.dut_serial if ctx else 'PENDING'
                hw_sn = ctx.dut_manifest.get('HW_SN_CARRIER', '') if ctx else ''
                hw_sn_part = f'_HW-{hw_sn}' if hw_sn else ''
                html_dir = getattr(config, '_mes_html_dir', Path('artifacts/evse_telemetry/html_reports'))
                html_dir.mkdir(parents=True, exist_ok=True)
                final_name = f'{status}_{time_str}_{safe_operator}_SN-{serial}{hw_sn_part}.html'
                final_path = html_dir / final_name
                shutil.move(htmlpath, final_path)
                logger.info('eol_certificate_html_saved_final_path', final_path=final_path)
                config.option.htmlpath = str(final_path)
            except Exception as e:
                logger.error('failed_to_rename_html_report_e', e=e)
        spool_dir = getattr(config, '_mes_telemetry_spool_dir', None)
        target_dir = getattr(config, '_mes_telemetry_target_dir', None)
        if spool_dir and target_dir and spool_dir.exists():
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(spool_dir, target_dir, dirs_exist_ok=True)
                logger.info('telemetry_spool_successfully_synced_to_target_dir', target_dir=target_dir)
            except Exception as e:
                logger.error('warning_failed_to_sync_telemetry_spool_to_nfs_target_dir_e', target_dir=target_dir, e=e)
                logger.error('data_is_preserved_locally_in_spool_dir', spool_dir=spool_dir)

def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    """
    Injects the active Hardware Manifest into the final Pytest console output.

    Args:
        terminalreporter: The pytest terminal reporter object.
        exitstatus: The exit status code.
        config: The Pytest configuration object.
    """
    bom = getattr(config, '_mes_bom', None)
    if not bom:
        return
    terminalreporter.section('Hardware Manifest (Station BOM)', sep='=', blue=True, bold=True)
    terminalreporter.write_line(f'Facility     : {bom.station_meta.facility}')
    terminalreporter.write_line(f'Jig ID       : {bom.station_meta.jig_id} ({bom.station_meta.environment.upper()})')
    telemetry_sink = getattr(config, '_mes_telemetry_sink', None)
    if telemetry_sink and telemetry_sink.context:
        ctx = telemetry_sink.context
        som_sn = ctx.dut_manifest.get('serial_number', ctx.dut_serial)
        board_sn = ctx.dut_manifest.get('evse_carrier_serial', 'UNKNOWN')
        hw_sn = ctx.dut_manifest.get('HW_SN_CARRIER', '')
        terminalreporter.write_line(f'Board Serial : {board_sn}')
        terminalreporter.write_line(f'SOM Serial   : {som_sn}')
        if hw_sn:
            terminalreporter.write_line(f'HW SN        : {hw_sn}')
        for k, v in ctx.dut_manifest.items():
            if k not in ['serial_number', 'evse_carrier_serial', 'HW_SN_CARRIER', 'custom_flags'] and v:
                pretty_key = k.replace('_', ' ').title()
                terminalreporter.write_line(f'{pretty_key:<12} : {v}')

    transports = []
    if bom.ssh_targets:
        transports.append('SSH')
    if bom.uart:
        transports.append('UART Serial')
    if bom.can_bus:
        transports.append('CAN Bus')
    if bom.ethernet:
        transports.append('Ethernet')
    terminalreporter.write_line(f"Transports   : {(', '.join(transports) if transports else 'None')}")
    peripherals = []
    if getattr(bom, 'e_stop', None) and getattr(bom.e_stop, 'enabled', False):
        peripherals.append('E-Stop Watchdog')
    if bom.psu_hardware:
        peripherals.append('Programmable PSU')
    if bom.usb_sd_mux:
        peripherals.append('USB-SD-Mux')
    if bom.gpio_edge or bom.gpio_led or getattr(bom, 'bootstrap', None):
        peripherals.append('GPIO Rig')
    if bom.hid_scanners:
        peripherals.append('Barcode Scanner')
    terminalreporter.write_line(f"Peripherals  : {(', '.join(peripherals) if peripherals else 'None')}")
    provisioning = []
    if bom.tezi_provisioning:
        provisioning.append('TEZI (NXP uuu)')
    if bom.block_storage:
        provisioning.append('Block Flash (bmaptool)')
    if bom.microchip_targets:
        provisioning.append('Microchip ICP')
    terminalreporter.write_line(f"Provisioning : {(', '.join(provisioning) if provisioning else 'None')}")