"""
Core Configuration & Session Lifecycle Plugin

This module acts as the initialization layer for the pytest-mes-core framework.
It manages command-line argument parsing, hardware configuration validation (via TOML),
and the global execution lifecycle, including safety systems and telemetry.
"""

import pytest
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pytest_mes_core.config import StationEnvironment, load_toml_config
from pytest_mes_core.host_adapters.safety import EStopWatchdog
from pytest_mes_core.telemetry import StationContext, JsonlTelemetryExporter, TelemetryExporter

logger = logging.getLogger("mes_core.config")

# Globals for Session Lifecycle
_global_watchdog: Optional[EStopWatchdog] = None
_global_telemetry_sink: Optional[TelemetryExporter] = None

def pytest_addoption(parser: pytest.Parser) -> None:
    """
    Registers custom command-line arguments for the MES framework.

    Args:
        parser (pytest.Parser): The Pytest CLI argument parser.
    """
    group = parser.getgroup("mes_core", "Manufacturing Execution System Core")
    group.addoption(
        "--operator-id",
        action="store",
        required=True,
        help="The ID of the technician or CI pipeline running the test (Required for traceability)."
    )
    group.addoption(
        "--env-config",
        action="store",
        default="station_env.toml",
        help="Path to the TOML hardware configuration file."
    )
    group.addoption(
        "--generate-mes-config",
        action="store_true",
        help="Generates a default TOML template and exits."
    )
    group.addoption(
        "--hold-on-fail",
        action="store_true",
        help="Ergonomic debugging flag. Pauses execution and keeps hardware powered on if a test fails."
    )

@pytest.fixture(scope="session")
def operator_id(request: pytest.FixtureRequest) -> str:
    """
    Retrieves the operator identifier passed via the command-line interface.

    In a Manufacturing Execution System (MES), traceability is paramount. This
    fixture captures the '--operator-id' argument to track exactly who (or which
    CI/CD pipeline) executed the test batch. This ID is automatically injected into
    the telemetry context, allowing factory managers to correlate yield drops with
    specific shifts or personnel.

    Returns:
        str: The alphanumeric string representing the active operator or pipeline runner.
    """
    return str(request.config.getoption("--operator-id"))

@pytest.fixture(scope="session")
def mes_env(request: pytest.FixtureRequest) -> StationEnvironment:
    """
    Parses the hardware TOML configuration into a strongly typed Python object.

    This fixture is the single source of truth for the entire physical test setup.
    It reads the TOML file passed via the '--env-config' CLI argument and validates
    it against the StationEnvironment model. By injecting this fixture into your tests,
    you avoid hardcoding IP addresses, baud rates, or GPIO pins directly into test scripts.

    Returns:
        StationEnvironment: The validated hardware Bill of Materials (BOM).

    Example:
        def test_rs485_communication(mes_env, dut_transport):
            # Fetch parameters dynamically from the TOML configuration
            baud = mes_env.uart["onboard_rs485_0"].baudrate
            port = mes_env.uart["onboard_rs485_0"].port

            assert baud == 115200, "Jig configuration error: Expected 115200 baud."
            dut_transport.safe_run(f"stty -F {port} {baud}")
    """
    toml_path = Path(request.config.getoption("--env-config"))
    return load_toml_config(toml_path, StationEnvironment)

@pytest.fixture(scope="session")
def telemetry_sink() -> Optional[TelemetryExporter]:
    """
    Provides direct access to the active telemetry pipeline exporter.

    While standard pass/fail and execution duration metrics are automatically
    captured by the 'mes_record' wrapper, test engineers often need to attach
    highly specific, non-standard payload data to the JSONL record mid-test.

    Returns:
        Optional[TelemetryExporter]: The active exporter session, or None if disabled.

    Example:
        def test_mac_address_programming(telemetry_sink, dut_transport):
            res = dut_transport.safe_run("cat /sys/class/net/eth0/address")
            mac_address = res.stdout.strip()

            # Dynamically attach this custom data to the active test record
            if telemetry_sink:
                telemetry_sink.context.custom_data["provisioned_mac"] = mac_address

            assert len(mac_address) == 17
    """
    return _global_telemetry_sink

def pytest_configure(config: pytest.Config) -> None:
    """
    The Master Setup Hook. Executes once before any test collection begins.

    Responsibilities:
    1. Registers the 'requires_state' marker to prevent Pytest warnings.
    2. Dynamically routes CLI verbosity (-v, -vv) to live logging outputs.
    3. Engages the EStopWatchdog to safely apply mains power to the jig.
    4. Generates a unique Run UUID and initializes the JSONL artifact file.

    Args:
        config (pytest.Config): The active Pytest configuration object.
    """
    config.addinivalue_line(
        "markers", "requires_state(state): Enforces physical hardware state (DutState) before test execution."
    )

    config.option.log_cli = True
    config.option.log_cli_format = "%(asctime)s [%(levelname)7s] %(name)s: %(message)s"
    config.option.log_cli_date_format = "%H:%M:%S"

    config.addinivalue_line(
        "markers", "hardware_retry(retries): If a test fails, marks hardware DIRTY, forces a cold-boot, and retries."
    )

    verbosity = config.getoption("verbose")
    if verbosity == 0:
        config.option.log_cli_level = "WARNING"
        logging.getLogger("transitions").setLevel(logging.WARNING)
        logging.getLogger("paramiko").setLevel(logging.WARNING)
    elif verbosity == 1:
        config.option.log_cli_level = "INFO"
        logging.getLogger("transitions").setLevel(logging.INFO)
        logging.getLogger("paramiko").setLevel(logging.INFO)
    else:
        config.option.log_cli_level = "DEBUG"
        logging.getLogger("transitions").setLevel(logging.DEBUG)
        logging.getLogger("paramiko").setLevel(logging.DEBUG)

    toml_path = Path(config.getoption("--env-config"))
    if toml_path.exists():
        try:
            bom = load_toml_config(toml_path, StationEnvironment)

            if bom.e_stop and bom.e_stop.enabled:
                global _global_watchdog
                _global_watchdog = EStopWatchdog(bom.e_stop)
                _global_watchdog.__enter__()

            global _global_telemetry_sink
            session_id = str(uuid.uuid4())
            ctx = StationContext(
                facility=bom.station_meta.facility,
                jig_id=bom.station_meta.jig_id,
                operator_id=config.getoption("--operator-id", default="UNKNOWN")
            )
            setattr(ctx, "run_id", session_id)

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
                metadata["Run UUID"] = session_id
                metadata["Test Timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            logger.info(f"[Framework] Bootstrapping MES Session for Jig: {bom.station_meta.jig_id}")

        except Exception as e:
            logger.critical(f"[Framework] FATAL: Failed to load Hardware BOM: {e}")

def pytest_unconfigure(config: pytest.Config) -> None:
    """
    The Master Teardown Hook. Executes unconditionally after all tests finish
    or if the framework crashes fatally.

    Responsibilities:
    1. Safely trips the EStopWatchdog to drop high voltage from the jig.
    2. Flushes the Telemetry exporter buffers to disk to prevent data loss.

    Args:
        config (pytest.Config): The active Pytest configuration object.
    """
    global _global_watchdog, _global_telemetry_sink

    if _global_watchdog:
        _global_watchdog.__exit__(None, None, None)

    if _global_telemetry_sink:
        tests_failed = bool(config.pluginmanager.get_plugin("session").testsfailed)
        _global_telemetry_sink.end_session(session_passed=not tests_failed)
