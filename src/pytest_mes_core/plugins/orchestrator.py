"""
State Machine Orchestrator Plugin

This module integrates the core Finite State Machine (FSM) into the Pytest lifecycle.
It provides the master session-level state machine and an automatic wrapper fixture
that enforces physical hardware states before a test is allowed to execute.
"""

import pytest
import logging
from typing import Generator, Optional

from pytest_mes_core.config import StationEnvironment
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply
from pytest_mes_core.transports import EphemeralSSHClient, EphemeralSerialClient
from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine

logger = logging.getLogger("mes_core.orchestrator")

@pytest.fixture(scope="session")
def dut_state_machine(
    mes_env: StationEnvironment,
    psu_hardware: Optional[ScpiPowerSupply],
    serial_client: Optional[EphemeralSerialClient],
    ssh_client: Optional[EphemeralSSHClient]
) -> Generator[Optional[EmbeddedLinuxStateMachine], None, None]:
    """
    Initializes the master session State Machine for the Device Under Test.

    This fixture consumes the physical transports (PSU, Serial, SSH) and binds
    them to the state machine logic. It governs the physical execution context
    for the entire test run.

    Returns:
        Generator[Optional[EmbeddedLinuxStateMachine], None, None]: The active FSM,
        or None if disabled in the TOML configuration.

    Example:
        def test_force_dirty_state(dut_state_machine):
            # If a test intentionally corrupts the filesystem, mark it dirty
            # so the framework forces a hard reboot before the next test.
            dut_state_machine.machine.mark_dirty()
    """
    if not hasattr(mes_env, "state_machine") or not mes_env.state_machine or not mes_env.state_machine.enabled:
        yield None
        return

    if not serial_client or not ssh_client:
        logger.warning("[State Machine] Missing required UART or SSH transports. State machine disabled.")
        yield None
        return

    boot_profiler_cfg = mes_env.boot_profilers.get("linux_boot") if mes_env.boot_profilers else None

    sm = EmbeddedLinuxStateMachine(
        psu=psu_hardware,
        serial=serial_client,
        ssh=ssh_client,
        cfg=mes_env.state_machine,
        boot_profiler_cfg=boot_profiler_cfg
    )

    yield sm

    # Teardown: Print boot metrics for the run, then secure the hardware
    if sm.boot_metrics:
        logger.info(f"[Metrics] Final Boot Performance: {sm.boot_metrics}")

    sm.power_off()

@pytest.fixture(autouse=True)
def enforce_physical_state(
    request: pytest.FixtureRequest,
    dut_state_machine: Optional[EmbeddedLinuxStateMachine]
) -> Generator[None, None, None]:
    """
    The Master Hardware Router.

    This fixture runs automatically before EVERY test function. It inspects the
    test for a '@pytest.mark.requires_state(...)' decorator. If found, it compares
    the requested state against the board's current physical state and commands
    the FSM to transition the hardware accordingly (e.g., triggering a reboot to U-Boot).

    If a test fails critically (an assertion is thrown), this hook intercepts the
    failure during teardown and marks the State Machine as DIRTY, guaranteeing
    the next test starts from a clean, hard-booted environment.

    Returns:
        Generator[None, None, None]: Yields to the test body once the hardware is ready.
    """
    if not dut_state_machine:
        # Fallback: Run standard test flow if FSM is disabled
        yield
        return

    marker = request.node.get_closest_marker("requires_state")

    # Safely extract the Target State as a String
    target_state_name = marker.args[0].name if marker and marker.args else 'OS_USERLAND'

    # Safely extract the Current State from the FSM Enum as a String
    current_state_name = dut_state_machine.state.name if hasattr(dut_state_machine.state, 'name') else str(dut_state_machine.state)

    # State Routing Logic
    if current_state_name == target_state_name:
        logger.debug(f"[Router] Board is already in {current_state_name}. Bypassing boot sequence.")
    elif target_state_name == 'POWER_OFF':
        dut_state_machine.power_off()
    elif target_state_name == 'ENERGIZED':
        dut_state_machine.energize()
    elif target_state_name == 'BOOTLOADER':
        dut_state_machine.boot_to_bootloader()
    elif target_state_name == 'OS_USERLAND':
        dut_state_machine.boot_to_os()

    # Yield control to the actual test function
    yield

    # Forensic Check: Intercept failures and poison the FSM state
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call and rep_call.failed:
        dut_state_machine.mark_dirty()
        logger.warning(f"[Router] Test '{request.node.name}' failed. State marked DIRTY.")
