"""
Unit tests for AsyncUdpLogReceiver (Fix 8):
  a) cancellation must propagate out of _listen_loop (not be swallowed).
  b) wait_for_regex must register its subscriber BEFORE scanning
     history, closing the race where a packet arriving between the scan
     and registration was missed entirely.
  c) self._logs must be a bounded collections.deque(maxlen=50_000), not an
     unbounded list.
"""
import collections
import socket
import time
import threading
import pytest
from pytest_mes_core.config.protocols import UdpDiagnosticConfig
from pytest_mes_core.transports.base import TransportTimeoutError
from pytest_mes_core.transports.udp_logger import AsyncUdpLogReceiver

def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port

def _make_receiver(**cfg_kwargs) -> AsyncUdpLogReceiver:
    cfg = UdpDiagnosticConfig(bind_port=_free_udp_port(), **cfg_kwargs)
    return AsyncUdpLogReceiver(cfg)

def test_logs_is_bounded_deque_with_maxlen_50000():
    receiver = _make_receiver()
    assert isinstance(receiver._logs, collections.deque)
    assert receiver._logs.maxlen == 50000

def test_logs_deque_evicts_oldest_past_maxlen():
    receiver = _make_receiver()
    for i in range(50010):
        receiver._logs.append(f'line-{i}')
    assert len(receiver._logs) == 50000
    assert receiver._logs[0] == 'line-10'
    assert receiver._logs[-1] == 'line-50009'

def test_wait_for_regex_matches_historical_log():
    """A line that arrived before wait_for_regex was called must still be
    found via the history scan (reordering must not break this)."""
    receiver = _make_receiver()
    receiver._logs.append('boot: kernel panic detected')

    result = receiver.wait_for_regex('kernel panic')
    assert result == 'boot: kernel panic detected'

def test_wait_for_regex_subscriber_registered_before_history_scan():
    """The subscriber list must contain an entry for the duration of the call
    — proving registration happens (and happens before any awaiting on the
    stream), independent of whether a historical match already exists."""
    receiver = _make_receiver()
    receiver._logs.append('irrelevant line')
    seen_subscriber_during_scan = {'value': None}
    original_logs = receiver._logs

    class _SpyDeque(collections.deque):

        def __iter__(self):
            seen_subscriber_during_scan['value'] = len(receiver._subscribers)
            return super().__iter__()
    receiver._logs = _SpyDeque(original_logs, maxlen=original_logs.maxlen)

    with pytest.raises(TransportTimeoutError):
        receiver.wait_for_regex('NEVER_MATCHES_ANYTHING', 0.2)

    assert seen_subscriber_during_scan['value'] == 1
    assert len(receiver._subscribers) == 0

def test_wait_for_regex_times_out_and_cleans_up_subscriber():
    receiver = _make_receiver()

    with pytest.raises(TransportTimeoutError):
        receiver.wait_for_regex('NEVER_MATCHES', 0.2)

    assert len(receiver._subscribers) == 0

def test_start_stop_lifecycle_completes_promptly_without_hanging():
    """stop() cancels the listener; _listen_loop must let the cancellation
    exception propagate so the CancelScope actually observes and clears it.
    If cancellation were swallowed in a way that broke propagation semantics,
    this would risk the task group hanging or raising unexpectedly."""
    port = _free_udp_port()
    cfg = UdpDiagnosticConfig(bind_port=port, bind_address='127.0.0.1')
    receiver = AsyncUdpLogReceiver(cfg)

    start = time.monotonic()
    receiver.start()
    time.sleep(0.2)
    receiver.stop()
    assert time.monotonic() - start < 5.0

def test_end_to_end_live_udp_packet_matches_via_subscriber_stream():
    """Drives the real _listen_loop over a loopback UDP socket: start the
    receiver, fire a datagram at it after the wait has begun, and confirm
    wait_for_regex sees it via the live-subscriber path (not history)."""
    port = _free_udp_port()
    cfg = UdpDiagnosticConfig(bind_port=port, bind_address='127.0.0.1')
    receiver = AsyncUdpLogReceiver(cfg)

    def send_packet_after_delay():
        time.sleep(0.3)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(b'PANIC: oops\n', ('127.0.0.1', port))
        finally:
            sock.close()

    receiver.start()
    time.sleep(0.2)
    t = threading.Thread(target=send_packet_after_delay)
    t.start()
    
    result = receiver.wait_for_regex('PANIC', 5.0)
    assert result == 'PANIC: oops'
    t.join()
    receiver.stop()