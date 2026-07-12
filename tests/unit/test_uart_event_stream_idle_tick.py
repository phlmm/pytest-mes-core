"""
Fix 8: A silent UART (no bytes at all -- e.g. a broken TX wire so the DUT
never echoes anything) must not starve consumers whose timeout logic only
runs "on every event loop iteration".  UartEventStream.open()/open_async()
now emit a local-only IdleTick roughly once per second of RX silence so
those deadline checks keep running even when nothing is ever received.
"""
import queue
import pytest
from unittest.mock import MagicMock

from pytest_mes_core.state_machine import UartEventStream
from pytest_mes_core.events import IdleTick
from pytest_mes_core.transports.constants import ANSI_ESCAPE_B, PANIC_PATTERN_B


def _silent_serial() -> MagicMock:
    """A serial stub whose subscribe() queue never receives anything."""
    serial = MagicMock()
    serial.subscribe.return_value = queue.Queue()
    return serial


def test_open_yields_idle_ticks_and_nothing_else_on_total_silence():
    serial = _silent_serial()
    stream = UartEventStream(serial=serial, ansi_pattern=ANSI_ESCAPE_B, panic_pattern=PANIC_PATTERN_B)

    events = list(stream.open(prompts={}, timeout_s=2.5, flush=False))

    assert len(events) >= 2
    assert all(isinstance(e, IdleTick) for e in events)
    serial.unsubscribe.assert_called_once()


@pytest.mark.anyio
async def test_open_async_yields_idle_ticks_and_nothing_else_on_total_silence():
    serial = _silent_serial()
    stream = UartEventStream(serial=serial, ansi_pattern=ANSI_ESCAPE_B, panic_pattern=PANIC_PATTERN_B)

    events = []
    async for event in stream.open_async(prompts={}, timeout_s=2.5, flush=False):
        events.append(event)

    assert len(events) >= 2
    assert all(isinstance(e, IdleTick) for e in events)
    serial.unsubscribe.assert_called_once()
