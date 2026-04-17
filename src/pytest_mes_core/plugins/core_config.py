"""
Core Configuration & Session Lifecycle Plugin

This module acts as the initialization layer for the pytest-mes-core framework.
It manages command-line argument parsing, hardware configuration validation (via TOML),
and the global execution lifecycle, including safety systems and telemetry.
"""

import os
import pytest
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pytest_mes_core.config import StationEnvironment, load_toml_config
from pytest_mes_core.host_adapters.safety import EStopWatchdog
from pytest_mes_core.telemetry import (
    StationContext, TelemetryExporter, JsonlTelemetryExporter,
    OperatorReceiptExporter, DeveloperMarkdownExporter, CompositeTelemetryExporter
)

logger = logging.getLogger("mes_core.config")

# Global state removed. We bind directly to the pytest Config object to support xdist
# and avoid session bombs across multiple pytest invocations.

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
    group.addoption(
        "--mock-hardware",
        action="store_true",
        default=False,
        help="Bypasses physical transports. Injects a Mock Transport for CI/CD pipeline testing."
    )

def pytest_load_initial_conftests(early_config: pytest.Config, parser: pytest.Parser, args: list[str]) -> None:
    """
    The Pre-Parse Hook.
    Intercepts the raw CLI arguments and injects the pytest-html flags BEFORE
    the plugin manager initializes, forcing pytest-html to wake up.
    """
    # Only inject if the user didn't manually pass a custom --html flag
    if not any(arg.startswith("--html") for arg in args):
        args.extend(["--html=.mes_tmp_report.html", "--self-contained-html"])

@pytest.fixture(scope="session")
def operator_id(request: pytest.FixtureRequest) -> str:
    """
    Retrieves the operator identifier passed via the command-line interface.
    """
    return str(request.config.getoption("--operator-id"))

@pytest.fixture(scope="session")
def mes_env(request: pytest.FixtureRequest) -> StationEnvironment:
    """
    Parses the hardware TOML configuration into a strongly typed Python object.
    """
    toml_path = Path(request.config.getoption("--env-config"))
    return load_toml_config(toml_path, StationEnvironment)

@pytest.fixture(scope="session")
def telemetry_sink(request: pytest.FixtureRequest) -> Optional[TelemetryExporter]:
    """
    Provides direct access to the active telemetry pipeline exporter.
    """
    return getattr(request.config, "_mes_telemetry_sink", None)

def pytest_configure(config: pytest.Config) -> None:
    """
    The Master Setup Hook. Executes once before any test collection begins.
    """
    config.addinivalue_line(
        "markers", "requires_state(state): Enforces physical hardware state (DutState) before test execution."
    )
    config.addinivalue_line(
        "markers", "hardware_retry(retries): If a test fails, marks hardware DIRTY, forces a cold-boot, and retries."
    )

    config.option.log_cli = True
    config.option.log_cli_format = "%(asctime)s [%(levelname)7s] %(name)s: %(message)s"
    config.option.log_cli_date_format = "%H:%M:%S"

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
                watchdog = EStopWatchdog(bom.e_stop)
                watchdog.__enter__()
                config._mes_watchdog = watchdog

            session_id = str(uuid.uuid4())
            ctx = StationContext(
                facility=bom.station_meta.facility,
                jig_id=bom.station_meta.jig_id,
                operator_id=config.getoption("--operator-id", default="UNKNOWN")
            )
            setattr(ctx, "run_id", session_id)

            if bom.telemetry.exporter_type == "jsonl":
                log_dir = Path(bom.telemetry.log_directory) if bom.telemetry.log_directory else Path("artifacts/evse_telemetry")

                # 1. Base Exporter (Always Active)
                active_exporters = [JsonlTelemetryExporter(log_dir)]

                # 2. Contextual Exporters based on TOML
                if bom.station_meta.environment in ["lab", "developer"]:
                    active_exporters.append(DeveloperMarkdownExporter(log_dir))
                else:
                    active_exporters.append(OperatorReceiptExporter(log_dir))

                # 3. Instantiate Router
                telemetry_sink = CompositeTelemetryExporter(active_exporters)
                try:
                    telemetry_sink.start_session(ctx)
                    config._mes_telemetry_sink = telemetry_sink
                except Exception as e:
                    logger.critical(f"FATAL: Telemetry sub-system failed to initialize! {e}")
                    pytest.exit(f"MES Framework aborted. Cannot guarantee telemetry storage: {e}", returncode=1)

                # 🚨 HTML EOL CERTIFICATE AUTO-CONFIG
                date_str = datetime.now().strftime("%Y-%m-%d")
                html_dir = log_dir / "html_reports" / date_str
                html_dir.mkdir(parents=True, exist_ok=True)

                config._mes_html_dir = html_dir

            # Metadata Injection for the HTML Report Header
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
    """
    watchdog = getattr(config, "_mes_watchdog", None)
    telemetry_sink = getattr(config, "_mes_telemetry_sink", None)

    if watchdog:
        watchdog.__exit__(None, None, None)

    if telemetry_sink:
        tests_failed = bool(config.pluginmanager.get_plugin("session").testsfailed)
        session_passed = not tests_failed

        # Flush the Telemetry exporter buffers
        telemetry_sink.end_session(session_passed=session_passed)

        #  DYNAMIC HTML REPORT RENAMING
        htmlpath = getattr(config.option, "htmlpath", None)
        if htmlpath and os.path.exists(htmlpath):
            try:
                ctx = telemetry_sink.context if telemetry_sink else None
                status = "PASS" if session_passed else "FAIL"
                time_str = datetime.now().strftime("%H-%M-%S")

                safe_operator = ctx.operator_id.replace("/", "_") if ctx else "UNKNOWN"
                serial = ctx.dut_serial if ctx else "PENDING"

                # Fetch the stashed target directory (or fallback if it somehow failed)
                html_dir = getattr(config, "_mes_html_dir", Path("artifacts/evse_telemetry/html_reports"))
                html_dir.mkdir(parents=True, exist_ok=True)

                final_name = f"{status}_{time_str}_{safe_operator}_SN-{serial}.html"
                final_path = html_dir / final_name

                # Move the temp file to the final Enterprise directory
                os.rename(htmlpath, final_path)
                logger.info(f"[MES] EOL Certificate (HTML) saved: {final_path}")
            except Exception as e:
                logger.error(f"[MES] Failed to rename HTML report: {e}")
