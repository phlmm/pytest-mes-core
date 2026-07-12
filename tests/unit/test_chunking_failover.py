# tests/unit/test_chunking_failover.py
"""
Fix 1 regression test: HostSideBuffer's failed-over harvest path used to call
len() on a generator (parser.extract_lines() returns a Generator[str, None, None]),
which raised a TypeError swallowed by a bare `except Exception: pass`. The lines
were captured into self._buffer (list comprehension consumed the generator before
the len() call), but self._lines_read never advanced past 0.
"""
import time
from unittest.mock import MagicMock

from pytest_mes_core.transports.chunking import HostSideBuffer
from pytest_mes_core.utils.uart_parser import UartStreamParser


class _FakeFallback:
    def __init__(self, parser):
        self.parser = parser


class _FakeFailedOverTransport:
    """Mimics a FailoverTransport that has already failed over to the UART
    fallback: is_failed_over=True and fallback.parser is a real UartStreamParser
    the watchdog is passively feeding."""

    def __init__(self, parser):
        self.is_failed_over = True
        self.fallback = _FakeFallback(parser)
        # safe_run should never be called on this path.
        self.safe_run = MagicMock(side_effect=AssertionError(
            "safe_run must not be called while is_failed_over is True"
        ))


def test_failover_harvest_advances_lines_read_and_captures_lines():
    parser = UartStreamParser()
    parser.ingest(b"line one\nline two\nline three\n")

    transport = _FakeFailedOverTransport(parser)
    buf = HostSideBuffer(transport, "/var/log/does-not-matter", poll_interval_s=0.1)

    buf.start()
    time.sleep(1.2)
    survived = buf.stop()

    assert survived == ["line one", "line two", "line three"]
    assert buf._lines_read == 3


def test_failover_harvest_accumulates_across_multiple_polls():
    parser = UartStreamParser()
    transport = _FakeFailedOverTransport(parser)
    buf = HostSideBuffer(transport, "/var/log/does-not-matter", poll_interval_s=0.1)

    buf.start()
    parser.ingest(b"first\n")
    time.sleep(0.3)
    parser.ingest(b"second\nthird\n")
    time.sleep(0.6)
    survived = buf.stop()

    assert survived == ["first", "second", "third"]
    assert buf._lines_read == 3
