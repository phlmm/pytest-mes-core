import pytest
from unittest.mock import MagicMock
from pytest_mes_core.mcu_state_machine import BareMetalStateMachine, McuState

def test_bare_metal_fsm_initial_state():
    fsm = BareMetalStateMachine()
    assert fsm.state == McuState.POWER_OFF.value

def test_bare_metal_fsm_energize_and_halt():
    psu = MagicMock()
    swd = MagicMock()
    fsm = BareMetalStateMachine(psu=psu, swd_transport=swd)
    
    # Energize
    fsm.energize()
    assert fsm.state == McuState.ENERGIZED.value
    psu.enable_output.assert_called_once()
    
    # Halt Core
    fsm.halt_core()
    assert fsm.state == McuState.HALTED.value
    swd.halt.assert_called_once()
    
    # Resume
    fsm.resume_core()
    assert fsm.state == McuState.RUNNING.value
    swd.resume.assert_called_once()

def test_bare_metal_fsm_ota_cycle():
    swd = MagicMock()
    fsm = BareMetalStateMachine(swd_transport=swd)
    
    fsm.energize()
    
    # Trigger OTA
    fsm.trigger_ota()
    assert fsm.state == McuState.OTA_UPDATE.value
    swd.reset.assert_called_once()
    
    # OTA Corrupt
    fsm.ota_corrupt()
    assert fsm.state == McuState.FAILED_OTA.value

@pytest.mark.anyio
async def test_async_hw_halt_core():
    swd = MagicMock()
    fsm = BareMetalStateMachine(swd_transport=swd)
    fsm.energize()
    
    await fsm.async_hw_halt_core()
    assert fsm.state == McuState.HALTED.value
    swd.halt.assert_called_once()
