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
    fsm.energize()
    assert fsm.state == McuState.ENERGIZED.value
    psu.enable_output.assert_called_once()
    fsm.halt_core()
    assert fsm.state == McuState.HALTED.value
    swd.halt.assert_called_once()
    fsm.resume_core()
    assert fsm.state == McuState.RUNNING.value
    swd.resume.assert_called_once()

def test_bare_metal_fsm_ota_cycle():
    swd = MagicMock()
    fsm = BareMetalStateMachine(swd_transport=swd)
    fsm.energize()
    fsm.trigger_ota()
    assert fsm.state == McuState.OTA_UPDATE.value
    swd.reset.assert_called_once()
    fsm.ota_corrupt()
    assert fsm.state == McuState.FAILED_OTA.value

def test_async_hw_halt_core():
    swd = MagicMock()
    fsm = BareMetalStateMachine(swd_transport=swd)
    fsm.energize()
    fsm.halt_core()
    assert fsm.state == McuState.HALTED.value
    swd.halt.assert_called_once()

def test_trigger_ota_writes_flag_before_reset():
    """Fix 8: when ota_flag_addr/ota_flag_value are configured, trigger_ota
    must actually write the magic flag via write_memory() before resetting —
    previously the docstring promised this but the code only called reset()."""
    swd = MagicMock()
    fsm = BareMetalStateMachine(swd_transport=swd, ota_flag_addr=536870912, ota_flag_value=b'\xef\xbe\xad\xde')
    fsm.energize()
    fsm.trigger_ota()
    assert fsm.state == McuState.OTA_UPDATE.value
    swd.write_memory.assert_called_once_with(536870912, b'\xef\xbe\xad\xde')
    swd.reset.assert_called_once()
    write_call_index = swd.method_calls.index(('write_memory', (536870912, b'\xef\xbe\xad\xde'), {}))
    reset_call_index = swd.method_calls.index(('reset', (), {}))
    assert write_call_index < reset_call_index

def test_trigger_ota_without_flag_config_only_resets():
    """When ota_flag_addr/value are not configured, no write_memory call is
    made — the caller is responsible for having set the flag some other way."""
    swd = MagicMock()
    fsm = BareMetalStateMachine(swd_transport=swd)
    fsm.energize()
    fsm.trigger_ota()
    assert fsm.state == McuState.OTA_UPDATE.value
    swd.write_memory.assert_not_called()
    swd.reset.assert_called_once()