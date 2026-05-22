import structlog
"""
State Machine Orchestrator Plugin

This module integrates the core Finite State Machine (FSM) into the Pytest lifecycle.
It provides the master session-level state machine and an automatic wrapper fixture
that enforces physical hardware states before a test is allowed to execute.
"""
import pytest
import anyio
import logging
from datetime import datetime, timezone
from typing import Generator, Optional
from pytest_mes_core.config import StationEnvironment
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply
from pytest_mes_core.transports import EphemeralSSHClient, EphemeralSerialClient
from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine
logger = structlog.get_logger('mes_core.orchestrator')

@pytest.fixture(scope='session')
def dut_state_machine(request: pytest.FixtureRequest, mes_env: StationEnvironment, psu_hardware: Optional[ScpiPowerSupply], serial_client: Optional[EphemeralSerialClient], ssh_client: Optional[EphemeralSSHClient]) -> Generator[Optional[EmbeddedLinuxStateMachine], None, None]:
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
    if not hasattr(mes_env, 'state_machine') or not mes_env.state_machine or (not mes_env.state_machine.enabled):
        yield None
        return
    if not serial_client or not ssh_client:
        logger.warning('[State Machine] Missing required UART or SSH transports. State machine disabled.')
        yield None
        return
    boot_profiler_cfg = mes_env.boot_profilers.get('linux_boot') if mes_env.boot_profilers else None
    sm = EmbeddedLinuxStateMachine(psu=psu_hardware, serial=serial_client, ssh=ssh_client, cfg=mes_env.state_machine, boot_profiler_cfg=boot_profiler_cfg, verbose=request.config.getoption('verbose') >= 1)
    try:
        yield sm
    finally:
        if sm.boot_metrics:
            logger.info('final_boot_performance_boot_metrics', boot_metrics=sm.boot_metrics)
            sink = getattr(request.config, '_mes_telemetry_sink', None)
            if sink:
                from pytest_mes_core.telemetry.base import TestRecord
                record = TestRecord(test_name='mes_fsm_boot_profiler', passed=True, duration_s=sm.boot_metrics.get('t_boot_total_to_shell_s', 0.0), metrics=sm.boot_metrics, context={})
                sink.emit_record(record)
        try:
            anyio.run(sm.power_off)
        except KeyboardInterrupt:
            logger.warning("power_off_interrupted_by_ctrl_c", action="teardown_continuing")
        except Exception as e:
            logger.error("power_off_failed_during_teardown", error=str(e))

@pytest.fixture(autouse=True)
def enforce_physical_state(request: pytest.FixtureRequest, dut_state_machine: Optional[EmbeddedLinuxStateMachine]) -> Generator[None, None, None]:
    """
    The Master Hardware Router.

    This fixture runs automatically before EVERY test function. It inspects the
    test for a '@pytest.mark.requires_state(...)' decorator. If found, it compares
    the requested state against the board's current physical state and commands
    the FSM to transition the hardware accordingly (e.g., triggering a reboot to U-Boot).

    If a test fails critically (an assertion is thrown), this hook intercepts the
    failure during teardown and marks the State Machine as DIRTY, guaranteeing
    the next test starts from a clean, hard-booted environment.

    Args:
        request: The Pytest fixture request.
        dut_state_machine: The active State Machine fixture.

    Returns:
        Generator[None, None, None]: Yields to the test body once the hardware is ready.
    """
    if not dut_state_machine:
        yield
        return
    marker = request.node.get_closest_marker('requires_state')
    is_async = request.node.get_closest_marker('anyio') is not None
    
    if is_async and marker:
        logger.warning('[Router] requires_state marker ignored on anyio test. You must use await fsm.async_hw_boot_to_os() directly in your test body.')
        
    if marker and not is_async:
        target_state_name = marker.args[0].name if marker and marker.args else 'OS_USERLAND'
        current_state_name = dut_state_machine.state.name if hasattr(dut_state_machine.state, 'name') else str(dut_state_machine.state)
        if current_state_name == target_state_name:
            if target_state_name == 'OS_USERLAND' and hasattr(dut_state_machine, 'verify_heartbeat'):
                if not dut_state_machine.verify_heartbeat():
                    logger.warning('[Router] Target is OS_USERLAND but heartbeat failed! Marking DIRTY and rebooting.')
                    anyio.run(dut_state_machine.mark_dirty)
                    anyio.run(dut_state_machine.boot_to_os)
                else:
                    logger.debug('board_is_already_in_current_state_name_and_heartbeat_ok_bypassing_boot_sequence', current_state_name=current_state_name)
            else:
                logger.debug('board_is_already_in_current_state_name_bypassing_boot_sequence', current_state_name=current_state_name)
        elif target_state_name == 'POWER_OFF':
            anyio.run(dut_state_machine.power_off)
        elif target_state_name == 'ENERGIZED':
            anyio.run(dut_state_machine.energize)
        elif target_state_name == 'BOOTLOADER':
            anyio.run(dut_state_machine.boot_to_bootloader)
        elif target_state_name == 'OS_USERLAND':
            anyio.run(dut_state_machine.boot_to_os)
            
    yield
    rep_call = getattr(request.node, 'rep_call', None)
    if rep_call and rep_call.failed:
        if hasattr(dut_state_machine.machine, 'get_graph'):
            try:
                import os
                os.makedirs('artifacts', exist_ok=True)
                clean_name = request.node.name.replace('/', '_').replace(':', '_').replace('[', '_').replace(']', '')
                graph_path = f'artifacts/fsm_crash_{clean_name}.png'
                dut_state_machine.machine.get_graph().draw(graph_path, prog='dot')
                logger.critical('crash_graph_generated_graph_path', graph_path=graph_path)
            except Exception as e:
                logger.debug('failed_to_generate_graphviz_image_e', e=e)
        anyio.run(dut_state_machine.mark_dirty)
        logger.warning('test_name_failed_state_marked_dirty', name=request.node.name)

def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """
    Translates the MES-specific hardware_retry marker into the pytest-rerunfailures engine.
    This keeps the core framework API decoupled from third-party plugins.

    Args:
        config: The Pytest configuration object.
        items: The list of collected test items.
    """
    for item in items:
        retry_marker = item.get_closest_marker('hardware_retry')
        if retry_marker:
            retries = 1
            if retry_marker.args:
                retries = retry_marker.args[0]
            elif 'retries' in retry_marker.kwargs:
                retries = retry_marker.kwargs['retries']
            item.add_marker(pytest.mark.flaky(reruns=retries))