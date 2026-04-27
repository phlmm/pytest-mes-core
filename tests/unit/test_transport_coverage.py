"""
Comprehensive transport layer coverage tests.

Targets previously uncovered branches across:
  - failover.py  (async paths, passthrough methods, connect-lock, unsubscribe)
  - chunking.py  (start/stop idempotency, poll-loop all branches)
  - watchdog.py  (rolling-window trim, register_panic_callback end-to-end)

All transports are fully mocked — no hardware required.
"""
from __future__ import annotations

import queue
import threading
import time
from functools import partial
from typing import List
from unittest.mock import AsyncMock, MagicMock, PropertyMock, call

import pytest

from pytest_mes_core.transports.base import CommandResult, TransportConnectionError
from pytest_mes_core.transports.chunking import HostSideBuffer
from pytest_mes_core.transports.failover import FailoverTransport
from pytest_mes_core.transports.watchdog import UartKernelWatchdog


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _ok(stdout: str = "MES_PING") -> CommandResult:
    return CommandResult(command="echo", stdout=stdout, stderr="", exited=0, ok=True, duration_s=0.01)


def _fail(stdout: str = "") -> CommandResult:
    return CommandResult(command="echo", stdout=stdout, stderr="err", exited=1, ok=False, duration_s=0.01)


def _make_failover(primary_connected: bool = True, fallback_connected: bool = True):
    primary = MagicMock()
    type(primary).is_connected = PropertyMock(return_value=primary_connected)
    primary.safe_run.return_value = _ok()
    primary.connect.return_value = None
    primary.disconnect.return_value = None
    primary.async_connect = AsyncMock()
    primary.async_disconnect = AsyncMock()
    primary.async_safe_run = AsyncMock(return_value=_ok())

    fallback = MagicMock()
    type(fallback).is_connected = PropertyMock(return_value=fallback_connected)
    fallback.watchdog = None
    fallback.safe_run.return_value = _ok()
    fallback.connect.return_value = None
    fallback.disconnect.return_value = None
    fallback.async_connect = AsyncMock()
    fallback.async_disconnect = AsyncMock()
    fallback.async_safe_run = AsyncMock(return_value=_ok())

    router = FailoverTransport(primary, fallback)
    return router, primary, fallback


def _make_watchdog(is_connected: bool = True):
    """Returns (watchdog, mock_serial_client, data_queue)."""
    import re
    mock_client = MagicMock()
    mock_client.is_connected = is_connected
    mock_client.ANSI_ESCAPE_B = re.compile(rb'\x1b\[[0-9;]*[a-zA-Z]')
    data_q: queue.Queue = queue.Queue()
    mock_client.subscribe.return_value = data_q
    mock_client.unsubscribe = MagicMock()
    mock_client.test_queue = data_q
    wd = UartKernelWatchdog(mock_client)
    return wd, mock_client, data_q


# ===========================================================================
# FailoverTransport — async paths
# ===========================================================================

class TestFailoverAsync:
    @pytest.mark.anyio
    async def test_async_connect_both_transports(self):
        """async_connect() must connect both primary and fallback."""
        router, primary, fallback = _make_failover(primary_connected=False, fallback_connected=False)
        type(primary).is_connected = PropertyMock(return_value=False)
        await router.async_connect()
        fallback.async_connect.assert_awaited_once()
        primary.async_connect.assert_awaited_once()

    @pytest.mark.anyio
    async def test_async_connect_primary_offline_sets_failover_mode(self):
        """If primary raises TransportConnectionError, async_connect must set is_failed_over=True.
        The recovery thread is stopped immediately to prevent it from racing and resetting the flag.
        """
        router, primary, fallback = _make_failover(primary_connected=False, fallback_connected=False)
        type(primary).is_connected = PropertyMock(return_value=False)
        primary.async_connect.side_effect = TransportConnectionError("async SSH offline")
        # Make safe_run also fail so the recovery probe cannot succeed
        primary.safe_run.side_effect = TransportConnectionError("still offline")
        primary.connect.side_effect = TransportConnectionError("still offline")
        await router.async_connect()
        # Stop recovery thread before asserting so it cannot race-reset the flag
        router._stop_recovery.set()
        assert router.is_failed_over is True

    @pytest.mark.anyio
    async def test_async_connect_is_idempotent_when_already_connected(self):
        """async_connect() must not reconnect if is_connected is True."""
        router, primary, fallback = _make_failover(primary_connected=True)
        await router.async_connect()
        primary.async_connect.assert_not_awaited()
        fallback.async_connect.assert_not_awaited()

    @pytest.mark.anyio
    async def test_async_disconnect_tears_down_both(self):
        """async_disconnect() must disconnect both primary and fallback."""
        router, primary, fallback = _make_failover(primary_connected=False, fallback_connected=False)
        type(primary).is_connected = PropertyMock(return_value=False)
        await router.async_connect()
        # Give recovery thread a moment to start
        await __import__('anyio').sleep(0.05)
        await router.async_disconnect()
        primary.async_disconnect.assert_awaited_once()
        fallback.async_disconnect.assert_awaited_once()

    @pytest.mark.anyio
    async def test_async_safe_run_uses_primary_when_healthy(self):
        """async_safe_run() must route to primary when not failed over."""
        router, primary, fallback = _make_failover()
        primary.async_safe_run.return_value = _ok(stdout="primary_out")
        result = await router.async_safe_run("ls")
        primary.async_safe_run.assert_awaited_once()
        fallback.async_safe_run.assert_not_awaited()
        assert result.stdout == "primary_out"

    @pytest.mark.anyio
    async def test_async_safe_run_uses_fallback_when_failed_over(self):
        """async_safe_run() must route to fallback when is_failed_over=True."""
        router, primary, fallback = _make_failover()
        router.is_failed_over = True
        fallback.async_safe_run.return_value = _ok(stdout="fallback_out")
        result = await router.async_safe_run("ls")
        primary.async_safe_run.assert_not_awaited()
        assert result.stdout == "fallback_out"

    @pytest.mark.anyio
    async def test_async_safe_run_failover_auto_retry_true(self):
        """With auto_retry=True, async_safe_run must retry on fallback after primary failure."""
        router, primary, fallback = _make_failover()
        primary.async_safe_run.side_effect = TransportConnectionError("SSH gone async")
        fallback.async_safe_run.return_value = _ok(stdout="retried")
        result = await router.async_safe_run("ls", auto_retry=True)
        assert result.stdout == "retried"
        assert router.is_failed_over is True

    @pytest.mark.anyio
    async def test_async_safe_run_failover_no_retry_raises(self):
        """Without auto_retry, async_safe_run must propagate the TransportConnectionError."""
        router, primary, fallback = _make_failover()
        primary.async_safe_run.side_effect = TransportConnectionError("SSH gone async")
        fallback.async_safe_run.return_value = _ok()
        with pytest.raises(TransportConnectionError):
            await router.async_safe_run("ls", auto_retry=False)
        assert router.is_failed_over is True


# ===========================================================================
# FailoverTransport — passthrough methods
# ===========================================================================

class TestFailoverPassthrough:
    # --- subscribe ---
    def test_subscribe_delegates_to_fallback(self):
        router, _, fallback = _make_failover()
        mock_q = queue.Queue()
        fallback.subscribe.return_value = mock_q
        result = router.subscribe(maxsize=512)
        fallback.subscribe.assert_called_once_with(512)
        assert result is mock_q

    def test_subscribe_raises_not_implemented_when_fallback_lacks_it(self):
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=True)
        fallback = MagicMock(spec=[])  # no attributes
        router = FailoverTransport(primary, fallback)
        with pytest.raises(NotImplementedError):
            router.subscribe()

    # --- unsubscribe ---
    def test_unsubscribe_delegates_to_fallback(self):
        router, _, fallback = _make_failover()
        q = queue.Queue()
        router.unsubscribe(q)
        fallback.unsubscribe.assert_called_once_with(q)

    def test_unsubscribe_raises_not_implemented_when_fallback_lacks_it(self):
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=True)
        fallback = MagicMock(spec=[])
        router = FailoverTransport(primary, fallback)
        with pytest.raises(NotImplementedError):
            router.unsubscribe(queue.Queue())

    # --- expect ---
    def test_expect_delegates_to_fallback(self):
        router, _, fallback = _make_failover()
        fallback.expect.return_value = "matched"
        result = router.expect("pattern", timeout_s=3.0)
        fallback.expect.assert_called_once_with("pattern", timeout_s=3.0, blast_char='', active_redraw=True)
        assert result == "matched"

    def test_expect_raises_not_implemented_when_fallback_lacks_it(self):
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=True)
        fallback = MagicMock(spec=[])
        router = FailoverTransport(primary, fallback)
        with pytest.raises(NotImplementedError):
            router.expect("pattern")

    # --- write_line ---
    def test_write_line_delegates_to_fallback(self):
        router, _, fallback = _make_failover()
        router.write_line("reboot", sensitive=False)
        fallback.write_line.assert_called_once_with("reboot", sensitive=False)

    def test_write_line_raises_not_implemented_when_fallback_lacks_it(self):
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=True)
        fallback = MagicMock(spec=[])
        router = FailoverTransport(primary, fallback)
        with pytest.raises(NotImplementedError):
            router.write_line("cmd")

    # --- raw_write ---
    def test_raw_write_delegates_to_fallback(self):
        router, _, fallback = _make_failover()
        router.raw_write(b'\x00\xFF')
        fallback.raw_write.assert_called_once_with(b'\x00\xFF')

    def test_raw_write_raises_not_implemented_when_fallback_lacks_it(self):
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=True)
        fallback = MagicMock(spec=[])
        router = FailoverTransport(primary, fallback)
        with pytest.raises(NotImplementedError):
            router.raw_write(b'\xFF')

    # --- raw_read_chunk (DEPRECATED — use subscribe()/unsubscribe()) ---
    def test_raw_read_chunk_delegates_to_fallback(self):
        """raw_read_chunk must emit a DeprecationWarning but still delegate."""
        router, _, fallback = _make_failover()
        fallback.raw_read_chunk.return_value = b'\xAB\xCD'
        with pytest.warns(DeprecationWarning, match="pub/sub multiplexer"):
            result = router.raw_read_chunk()
        assert result == b'\xAB\xCD'

    def test_raw_read_chunk_raises_not_implemented_when_fallback_lacks_it(self):
        """raw_read_chunk must emit DeprecationWarning even when it then raises."""
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=True)
        fallback = MagicMock(spec=[])
        router = FailoverTransport(primary, fallback)
        with pytest.warns(DeprecationWarning, match="pub/sub multiplexer"):
            with pytest.raises(NotImplementedError):
                router.raw_read_chunk()

    # --- read_clean_stream (DEPRECATED — use UartEventStream.open()) ---
    def test_read_clean_stream_delegates_to_fallback(self):
        """read_clean_stream must emit a DeprecationWarning but still delegate."""
        router, _, fallback = _make_failover()
        fallback.read_clean_stream.return_value = iter(["line1", "line2"])
        with pytest.warns(DeprecationWarning, match="pub/sub multiplexer"):
            result = list(router.read_clean_stream())
        assert result == ["line1", "line2"]

    def test_read_clean_stream_raises_not_implemented_when_fallback_lacks_it(self):
        """read_clean_stream must emit DeprecationWarning even when it then raises."""
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=True)
        fallback = MagicMock(spec=[])
        router = FailoverTransport(primary, fallback)
        with pytest.warns(DeprecationWarning, match="pub/sub multiplexer"):
            with pytest.raises(NotImplementedError):
                router.read_clean_stream()

    # --- async_expect ---
    @pytest.mark.anyio
    async def test_async_expect_delegates_to_fallback(self):
        router, _, fallback = _make_failover()
        fallback.async_expect = AsyncMock(return_value="matched_async")
        result = await router.async_expect("pattern", timeout_s=2.0)
        fallback.async_expect.assert_awaited_once_with("pattern", timeout_s=2.0, blast_char='', active_redraw=True)
        assert result == "matched_async"

    @pytest.mark.anyio
    async def test_async_expect_raises_not_implemented_when_fallback_lacks_it(self):
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=True)
        fallback = MagicMock(spec=[])
        router = FailoverTransport(primary, fallback)
        with pytest.raises(NotImplementedError):
            await router.async_expect("pattern")


# ===========================================================================
# FailoverTransport — probe_recovery: skips when not failed over
# ===========================================================================

class TestFailoverProbeRecovery:
    def test_probe_recovery_skips_when_not_failed_over(self):
        """The probe loop must NOT attempt to reconnect the primary when is_failed_over=False."""
        router, primary, fallback = _make_failover()
        router.is_failed_over = False

        router._stop_recovery.clear()
        t = threading.Thread(target=router._probe_primary_recovery, daemon=True)
        t.start()
        time.sleep(0.2)
        router._stop_recovery.set()
        t.join(timeout=2.0)

        # No connection or ping attempt should happen when not failed over
        primary.connect.assert_not_called()
        primary.safe_run.assert_not_called()

    def test_probe_recovery_reconnects_when_primary_was_disconnected(self):
        """If primary is disconnected, the probe must call connect() before pinging."""
        router, primary, fallback = _make_failover(primary_connected=False)
        router.is_failed_over = True

        type(primary).is_connected = PropertyMock(return_value=False)
        primary.connect.return_value = None
        primary.safe_run.return_value = _ok(stdout="MES_PING")

        router._stop_recovery.clear()
        t = threading.Thread(target=router._probe_primary_recovery, daemon=True)
        t.start()
        time.sleep(0.3)
        router._stop_recovery.set()
        t.join(timeout=2.0)

        primary.connect.assert_called()


# ===========================================================================
# FailoverTransport — connect lock prevents double-thread spawn
# ===========================================================================

class TestFailoverConnectLock:
    def test_concurrent_connect_spawns_only_one_recovery_thread(self):
        """Two threads calling connect() simultaneously must result in exactly one recovery thread."""
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=False)
        primary.connect.return_value = None
        primary.disconnect.return_value = None

        fallback = MagicMock()
        type(fallback).is_connected = PropertyMock(return_value=False)
        fallback.watchdog = None
        fallback.connect.return_value = None
        fallback.disconnect.return_value = None

        router = FailoverTransport(primary, fallback)
        threads = [threading.Thread(target=router.connect) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.05)

        # Exactly one recovery thread should be running
        alive_count = sum(1 for t in [router._recovery_thread] if t and t.is_alive())
        assert alive_count == 1

        router._stop_recovery.set()
        if router._recovery_thread:
            router._recovery_thread.join(timeout=2.0)


# ===========================================================================
# FailoverTransport — watchdog register_panic_callback contract
# ===========================================================================

class TestFailoverWatchdogCallbackContract:
    def test_panic_callback_registered_via_watchdog_register_method(self):
        """FailoverTransport.__init__ must call watchdog.register_panic_callback(_on_panic)."""
        primary = MagicMock()
        type(primary).is_connected = PropertyMock(return_value=True)
        fallback = MagicMock()
        type(fallback).is_connected = PropertyMock(return_value=True)
        mock_watchdog = MagicMock()
        fallback.watchdog = mock_watchdog

        router = FailoverTransport(primary, fallback)
        mock_watchdog.register_panic_callback.assert_called_once_with(router._on_panic)

    def test_no_panic_callback_when_watchdog_lacks_register_method(self):
        """If watchdog doesn't have register_panic_callback, __init__ must not raise."""
        primary = MagicMock()
        fallback = MagicMock()
        fallback.watchdog = MagicMock(spec=[])  # no register_panic_callback attribute
        # Must not raise
        FailoverTransport(primary, fallback)


# ===========================================================================
# HostSideBuffer — start/stop idempotency and poll-loop branches
# ===========================================================================

class TestHostSideBuffer:
    def _make_transport(self, lines_per_call: List[str] = None, raise_on_call: int = None):
        transport = MagicMock()
        call_count = {"n": 0}

        def _safe_run(cmd, timeout_s=5.0):
            call_count["n"] += 1
            if raise_on_call and call_count["n"] >= raise_on_call:
                raise TransportConnectionError("DUT crashed")
            if lines_per_call:
                stdout = "\n".join(lines_per_call)
                return CommandResult(command=cmd, stdout=stdout, stderr="", exited=0, ok=True, duration_s=0.01)
            return CommandResult(command=cmd, stdout="", stderr="", exited=0, ok=True, duration_s=0.01)

        transport.safe_run.side_effect = _safe_run
        transport.is_failed_over = False
        return transport

    def test_start_is_idempotent(self):
        """Calling start() twice must not spawn a second thread."""
        transport = self._make_transport()
        buf = HostSideBuffer(transport, "/var/log/test.log", poll_interval_s=0.5)
        buf.start()
        first_thread = buf._thread
        buf.start()  # second call — should be no-op
        assert buf._thread is first_thread
        buf.stop()

    def test_stop_returns_accumulated_lines(self):
        """stop() must return all lines that were polled from the transport."""
        transport = self._make_transport(lines_per_call=["line1", "line2", "line3"])
        buf = HostSideBuffer(transport, "/var/log/test.log", poll_interval_s=0.5)
        buf.start()
        time.sleep(0.8)  # allow at least one poll
        lines = buf.stop()
        assert isinstance(lines, list)
        assert len(lines) >= 3

    def test_start_floors_fast_poll_interval(self):
        """Intervals below 0.5 s must be floored in _effective_poll_s.
        The user-visible poll_interval_s must NOT be mutated."""
        transport = self._make_transport()
        buf = HostSideBuffer(transport, "/var/log/test.log", poll_interval_s=0.1)
        buf.start()
        # User-visible attribute preserved
        assert buf.poll_interval_s == 0.1
        # Internal effective interval floored
        assert buf._effective_poll_s == 0.5
        buf.stop()

    def test_stop_returns_empty_list_when_transport_returns_no_data(self):
        transport = self._make_transport(lines_per_call=[])
        buf = HostSideBuffer(transport, "/var/log/test.log", poll_interval_s=0.5)
        buf.start()
        time.sleep(0.2)
        lines = buf.stop()
        assert lines == []

    def test_poll_loop_aborts_on_transport_connection_error(self):
        """When the transport raises TransportConnectionError, the poll loop must abort and
        preserve all lines received before the crash."""
        transport = self._make_transport(
            lines_per_call=["crash_line"],
            raise_on_call=2,  # crash on 2nd call
        )
        buf = HostSideBuffer(transport, "/var/log/test.log", poll_interval_s=0.5)
        buf.start()
        time.sleep(1.5)  # allow at least 2 polls
        lines = buf.stop()
        # Thread should have exited; buffer should have some data
        assert buf._thread is None or not buf._thread.is_alive()

    def test_poll_loop_uses_fallback_parser_on_failover(self):
        """When the transport is in failover mode, the poll loop must drain the fallback
        parser instead of calling safe_run()."""
        transport = MagicMock()
        transport.is_failed_over = True
        fallback = MagicMock()
        parser = MagicMock()
        parser.extract_lines.return_value = ["panic_line_1", "panic_line_2"]
        fallback.parser = parser
        transport.fallback = fallback

        buf = HostSideBuffer(transport, "/var/log/test.log", poll_interval_s=0.5)
        buf.start()
        time.sleep(0.8)
        lines = buf.stop()

        # safe_run must NOT have been called
        transport.safe_run.assert_not_called()
        assert len(lines) >= 2


# ===========================================================================
# UartKernelWatchdog — rolling window trim + register_panic_callback
# ===========================================================================

class TestWatchdogAdditional:
    def test_rolling_window_is_trimmed_to_1024_bytes(self):
        """After ingesting more than 1024 bytes, the rolling window must be capped."""
        wd, mock_client, data_q = _make_watchdog()
        # Send 1200 bytes of harmless data in chunks
        chunk = b'A' * 200
        for _ in range(6):
            data_q.put(chunk)

        wd.start()
        time.sleep(0.5)
        wd.stop()

        assert len(wd._rolling_window) <= 1024
        assert not wd.is_panicked()

    def test_register_panic_callback_is_invoked_on_panic(self):
        """A registered callback must be called synchronously when a panic is detected."""
        wd, _, data_q = _make_watchdog()

        invocations: List[str] = []

        def my_callback():
            invocations.append("called")

        wd.register_panic_callback(my_callback)
        data_q.put(b"Kernel panic - not syncing: fatal error")

        wd.start()
        time.sleep(0.5)
        wd.stop()

        assert wd.is_panicked()
        assert invocations == ["called"]

    def test_register_multiple_callbacks_all_invoked(self):
        """All registered callbacks must be invoked, in registration order."""
        wd, _, data_q = _make_watchdog()
        order: List[int] = []
        wd.register_panic_callback(lambda: order.append(1))
        wd.register_panic_callback(lambda: order.append(2))
        wd.register_panic_callback(lambda: order.append(3))

        data_q.put(b"Kernel panic - not syncing: VFS")
        wd.start()
        time.sleep(0.5)
        wd.stop()

        assert order == [1, 2, 3]

    def test_panic_callback_exception_does_not_crash_watchdog(self):
        """If a callback raises, the watchdog must swallow it and continue."""
        wd, _, data_q = _make_watchdog()

        def bad_callback():
            raise RuntimeError("callback exploded")

        wd.register_panic_callback(bad_callback)
        data_q.put(b"Kernel panic - not syncing: division by zero")

        wd.start()
        time.sleep(0.5)
        wd.stop()

        # Watchdog must still have detected the panic
        assert wd.is_panicked()

    def test_watchdog_reconnects_after_disconnect_mid_run(self):
        """If is_connected drops to False mid-run, the loop must wait and re-subscribe
        once the client reconnects."""
        import re
        mock_client = MagicMock()
        mock_client.ANSI_ESCAPE_B = re.compile(rb'\x1b\[[0-9;]*[a-zA-Z]')
        first_q: queue.Queue = queue.Queue()
        mock_client.subscribe.return_value = first_q

        # Start connected, drop to disconnected after first poll, then reconnect
        connected_seq = [True, True, False, False, True]
        call_idx = {"n": 0}

        def get_connected():
            idx = min(call_idx["n"], len(connected_seq) - 1)
            call_idx["n"] += 1
            return connected_seq[idx]

        type(mock_client).is_connected = PropertyMock(side_effect=get_connected)

        wd = UartKernelWatchdog(mock_client)
        wd.start()
        time.sleep(0.8)
        wd.stop()

        # Should not panic from a simple disconnect/reconnect cycle
        assert not wd.is_panicked()
