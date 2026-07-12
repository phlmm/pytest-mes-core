# tests/unit/test_udp_logger.py
"""
Unit tests for AsyncUdpLogReceiver (Fix 8):
  a) cancellation must propagate out of _listen_loop (not be swallowed).
  b) async_wait_for_regex must register its subscriber BEFORE scanning
     history, closing the race where a packet arriving between the scan
     and registration was missed entirely.
  c) self._logs must be a bounded collections.deque(maxlen=50_000), not an
     unbounded list.
"""
import collections
import socket
import time

import anyio
import pytest

from pytest_mes_core.config.protocols import UdpDiagnosticConfig
from pytest_mes_core.transports.base import TransportTimeoutError
from pytest_mes_core.transports.udp_logger import AsyncUdpLogReceiver


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_receiver(**cfg_kwargs) -> AsyncUdpLogReceiver:
    cfg = UdpDiagnosticConfig(bind_port=_free_udp_port(), **cfg_kwargs)
    return AsyncUdpLogReceiver(cfg)


# ===========================================================================
# Fix 8c: bounded deque
# ===========================================================================

def test_logs_is_bounded_deque_with_maxlen_50000():
    receiver = _make_receiver()
    assert isinstance(receiver._logs, collections.deque)
    assert receiver._logs.maxlen == 50_000


def test_logs_deque_evicts_oldest_past_maxlen():
    receiver = _make_receiver()
    for i in range(50_010):
        receiver._logs.append(f"line-{i}")
    assert len(receiver._logs) == 50_000
    # The first 10 lines must have been evicted (drop-oldest is deque's
    # native behavior once maxlen is set).
    assert receiver._logs[0] == "line-10"
    assert receiver._logs[-1] == "line-50009"


# ===========================================================================
# Fix 8b: async_wait_for_regex registers subscriber before scanning history
# ===========================================================================

def test_async_wait_for_regex_matches_historical_log():
    """A line that arrived before wait_for_regex was called must still be
    found via the history scan (reordering must not break this)."""
    receiver = _make_receiver()
    receiver._logs.append("boot: kernel panic detected")

    async def run():
        return await receiver.async_wait_for_regex(r"kernel panic")

    result = anyio.run(run)
    assert result == "boot: kernel panic detected"


def test_async_wait_for_regex_subscriber_registered_before_history_scan():
    """The subscriber list must contain an entry for the duration of the call
    — proving registration happens (and happens before any awaiting on the
    stream), independent of whether a historical match already exists."""
    receiver = _make_receiver()
    receiver._logs.append("irrelevant line")

    seen_subscriber_during_scan = {"value": None}
    original_logs = receiver._logs

    class _SpyDeque(collections.deque):
        def __iter__(self):
            seen_subscriber_during_scan["value"] = len(receiver._subscribers)
            return super().__iter__()

    receiver._logs = _SpyDeque(original_logs, maxlen=original_logs.maxlen)

    async def run():
        with pytest.raises(TransportTimeoutError):
            await receiver.async_wait_for_regex(r"NEVER_MATCHES_ANYTHING", timeout_s=0.2)

    anyio.run(run)
    # By the time the history scan iterated _logs, the subscriber must
    # already have been appended (len == 1), proving register-then-scan order.
    assert seen_subscriber_during_scan["value"] == 1
    # Subscriber must be cleaned up afterwards (finally block ran).
    assert len(receiver._subscribers) == 0


def test_async_wait_for_regex_times_out_and_cleans_up_subscriber():
    receiver = _make_receiver()

    async def run():
        with pytest.raises(TransportTimeoutError):
            await receiver.async_wait_for_regex(r"NEVER_MATCHES", timeout_s=0.2)

    anyio.run(run)
    assert len(receiver._subscribers) == 0


# ===========================================================================
# Fix 8a: cancellation propagates (end-to-end over a real loopback socket)
# ===========================================================================

def test_start_stop_lifecycle_completes_promptly_without_hanging():
    """stop() cancels the listener; _listen_loop must let the cancellation
    exception propagate so the CancelScope actually observes and clears it.
    If cancellation were swallowed in a way that broke propagation semantics,
    this would risk the task group hanging or raising unexpectedly."""
    port = _free_udp_port()
    cfg = UdpDiagnosticConfig(bind_port=port, bind_address="127.0.0.1")
    receiver = AsyncUdpLogReceiver(cfg)

    async def run():
        async with anyio.create_task_group() as tg:
            await receiver.start(tg)
            await anyio.sleep(0.2)  # let the socket bind
            await receiver.stop()

    start = time.monotonic()
    anyio.run(run)
    assert time.monotonic() - start < 5.0


def test_end_to_end_live_udp_packet_matches_via_subscriber_stream():
    """Drives the real _listen_loop over a loopback UDP socket: start the
    receiver, fire a datagram at it after the wait has begun, and confirm
    async_wait_for_regex sees it via the live-subscriber path (not history)."""
    port = _free_udp_port()
    cfg = UdpDiagnosticConfig(bind_port=port, bind_address="127.0.0.1")
    receiver = AsyncUdpLogReceiver(cfg)

    async def send_packet_after_delay():
        await anyio.sleep(0.3)
        sock = await anyio.create_udp_socket(family=socket.AF_INET, local_host="127.0.0.1", local_port=0)
        try:
            await sock.sendto(b"PANIC: oops\n", "127.0.0.1", port)
        finally:
            await sock.aclose()

    async def run():
        async with anyio.create_task_group() as tg:
            await receiver.start(tg)
            await anyio.sleep(0.2)  # let the socket bind
            tg.start_soon(send_packet_after_delay)
            result = await receiver.async_wait_for_regex(r"PANIC", timeout_s=5.0)
            assert result == "PANIC: oops"
            await receiver.stop()

    anyio.run(run)
