import functools
'Unit tests for pytest_mes_core.mcu_app_state.McuAppStateMachine.\n\nCovers Fix 7: the waiter wake-up path is now poll-based (thread-safe by\nconstruction, no TOCTOU registration race) and state_durations reports\nactual durations instead of raw entry timestamps.\n'
import threading
import time
import anyio
import pytest
from pytest_mes_core.mcu_app_state import McuAppStateMachine

@pytest.mark.anyio
async def test_async_wait_for_wakes_on_foreign_thread_transition():
    """The waiter must observe a transition fed from a different OS thread
    (as MQTT/UDP callbacks do in production, per this module's docstring)
    without deadlocking or sleeping through the wake-up."""
    fsm = McuAppStateMachine()

    def feed_from_thread() -> None:
        time.sleep(0.1)
        fsm.feed_event('boot_started')
        time.sleep(0.05)
        fsm.feed_event('network_up')
    t = threading.Thread(target=feed_from_thread)
    t.start()
    try:
        result = fsm.wait_for('NETWORK_UP', timeout_s=5.0, poll_interval_s=0.01)
    finally:
        t.join(timeout=5.0)
    assert result is True
    assert fsm.state == 'NETWORK_UP'

@pytest.mark.anyio
async def test_async_wait_for_times_out_when_state_never_reached():
    fsm = McuAppStateMachine()
    result = fsm.wait_for('APP_READY', timeout_s=0.2, poll_interval_s=0.02)
    assert result is False

@pytest.mark.anyio
async def test_async_wait_for_returns_immediately_if_already_in_target_state():
    fsm = McuAppStateMachine()
    result = fsm.wait_for('OFFLINE', timeout_s=1.0)
    assert result is True

@pytest.mark.anyio
async def test_state_durations_tracks_exited_and_current_state():
    """An exited state must accumulate a real elapsed duration (not an entry
    timestamp), and the currently-occupied state's running time must also be
    reported even though it has not been exited yet."""
    fsm = McuAppStateMachine()
    fsm.feed_event('boot_started')
    await anyio.sleep(0.05)
    fsm.feed_event('network_up')
    await anyio.sleep(0.05)
    durations = fsm.state_durations
    assert 0 < durations['BOOTING'] < 5.0
    assert 'NETWORK_UP' in durations
    assert durations['NETWORK_UP'] > 0
    await anyio.sleep(0.05)
    later = fsm.state_durations['NETWORK_UP']
    assert later > durations['NETWORK_UP']