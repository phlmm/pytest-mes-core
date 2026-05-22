import pytest
from unittest.mock import MagicMock
from pytest_mes_core.mcu_state_machine import BareMetalStateMachine, McuState

@pytest.mark.anyio
async def test_bare_metal_fsm_initial_state():
    fsm = BareMetalStateMachine()
    assert fsm.state == McuState.POWER_OFF.value

@pytest.mark.anyio
async def test_bare_metal_fsm_energize_and_halt():
    psu = MagicMock()
    swd = MagicMock()
    fsm = BareMetalStateMachine(psu=psu, swd_transport=swd)
    
    # Energize
    await fsm.energize()
    assert fsm.state == McuState.ENERGIZED.value
    psu.enable_output.assert_called_once()
    
    # Halt Core
    await fsm.halt_core()
    assert fsm.state == McuState.HALTED.value
    swd.halt.assert_called_once()
    
    # Resume
    await fsm.resume_core()
    assert fsm.state == McuState.RUNNING.value
    swd.resume.assert_called_once()

@pytest.mark.anyio
async def test_bare_metal_fsm_ota_cycle():
    swd = MagicMock()
    fsm = BareMetalStateMachine(swd_transport=swd)
    
    await fsm.energize()
    
    # Trigger OTA
    await fsm.trigger_ota()
    assert fsm.state == McuState.OTA_UPDATE.value
    swd.reset.assert_called_once()
    
    # OTA Corrupt
    await fsm.ota_corrupt()
    assert fsm.state == McuState.FAILED_OTA.value

@pytest.mark.anyio
async def test_async_hw_halt_core():
    swd = MagicMock()
    fsm = BareMetalStateMachine(swd_transport=swd)
    await fsm.energize()

    # halt_core fires the FSM transition (ENERGIZED -> HALTED) and
    # calls _hw_halt_core.
    await fsm.halt_core()
    assert fsm.state == McuState.HALTED.value
    swd.halt.assert_called_once()
