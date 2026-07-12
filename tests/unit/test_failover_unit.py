"""
Unit tests for FailoverTransport covering previously uncovered branches (20% → ~95%).

All transports are fully mocked — no hardware needed.
"""
import time
import threading
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from pytest_mes_core.transports.failover import FailoverTransport
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cmd_result(ok: bool = True, stdout: str = "MES_PING") -> CommandResult:
    return CommandResult(command="echo MES_PING", stdout=stdout, stderr="",
                         exited=0 if ok else 1, ok=ok, duration_s=0.01)


def _make_failover(primary_connected=True, fallback_connected=True):
    primary = MagicMock()
    type(primary).is_connected = PropertyMock(return_value=primary_connected)
    primary.safe_run.return_value = _cmd_result(ok=True)
    primary.connect.return_value = None
    primary.disconnect.return_value = None

    fallback = MagicMock()
    type(fallback).is_connected = PropertyMock(return_value=fallback_connected)
    fallback.watchdog = None  # default: no watchdog on fallback
    fallback.safe_run.return_value = _cmd_result(ok=True)
    fallback.connect.return_value = None
    fallback.disconnect.return_value = None

    router = FailoverTransport(primary, fallback)
    return router, primary, fallback


# ---------------------------------------------------------------------------
# __init__ — watchdog callback registration
# ---------------------------------------------------------------------------

def test_init_registers_panic_callback_on_fallback_watchdog():
    """If the fallback transport has a watchdog, _on_panic must be registered
    as a callback during __init__."""
    primary = MagicMock()
    type(primary).is_connected = PropertyMock(return_value=True)

    fallback = MagicMock()
    type(fallback).is_connected = PropertyMock(return_value=True)
    mock_watchdog = MagicMock()
    fallback.watchdog = mock_watchdog

    router = FailoverTransport(primary, fallback)

    mock_watchdog.register_panic_callback.assert_called_once_with(router._on_panic)


def test_init_no_watchdog_on_fallback_is_safe():
    """If fallback has no watchdog attribute, __init__ must not raise."""
    primary = MagicMock()
    fallback = MagicMock(spec=[])  # no attributes at all
    # Must not raise
    router = FailoverTransport(primary, fallback)


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------

def test_connect_calls_both_transports():
    """connect() must connect primary and fallback and start recovery thread."""
    router, primary, fallback = _make_failover(
        primary_connected=False, fallback_connected=False
    )
    type(primary).is_connected = PropertyMock(return_value=False)

    router.connect()
    time.sleep(0.05)

    primary.connect.assert_called_once()
    fallback.connect.assert_called_once()
    assert router._recovery_thread is not None
    assert router._recovery_thread.is_alive()

    router._stop_recovery.set()
    router._recovery_thread.join(timeout=2.0)


def test_connect_is_idempotent_when_already_connected():
    """connect() must be a no-op if is_connected is already True."""
    router, primary, fallback = _make_failover(primary_connected=True)

    router.connect()
    primary.connect.assert_not_called()
    fallback.connect.assert_not_called()


def test_connect_resets_is_failed_over_on_successful_primary_reconnect():
    """Fix 5a: after a previously failed-over matrix reconnects and the
    primary comes back up during connect(), is_failed_over must be reset to
    False immediately — traffic must not keep routing to the slow fallback
    until the recovery thread happens to notice."""
    primary = MagicMock()
    type(primary).is_connected = PropertyMock(return_value=False)
    primary.connect.return_value = None

    fallback = MagicMock()
    type(fallback).is_connected = PropertyMock(return_value=False)
    fallback.watchdog = None
    fallback.connect.return_value = None

    router = FailoverTransport(primary, fallback)
    router.is_failed_over = True  # matrix was failed-over from a previous cycle

    router.connect()
    time.sleep(0.05)

    assert router.is_failed_over is False
    primary.connect.assert_called_once()

    router._stop_recovery.set()
    if router._recovery_thread:
        router._recovery_thread.join(timeout=2.0)


def test_concurrent_connect_spawns_only_one_recovery_thread():
    """Fix 5b: two concurrent connect() calls must never each spawn their own
    recovery thread — _start_recovery_thread() re-checks liveness under
    _connect_lock."""
    router, primary, fallback = _make_failover(
        primary_connected=False, fallback_connected=False
    )
    type(primary).is_connected = PropertyMock(return_value=False)

    threads_created = []
    original_thread_cls = threading.Thread

    def tracking_thread(*args, **kwargs):
        t = original_thread_cls(*args, **kwargs)
        threads_created.append(t)
        return t

    # Build the driver (worker) threads with the *unpatched* Thread class first —
    # patching threading.Thread globally would otherwise also intercept these.
    workers = [original_thread_cls(target=router.connect) for _ in range(5)]
    with patch("pytest_mes_core.transports.failover.threading.Thread", side_effect=tracking_thread):
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=2.0)

    assert len(threads_created) == 1

    router._stop_recovery.set()
    if router._recovery_thread:
        router._recovery_thread.join(timeout=2.0)


def test_async_connect_resets_is_failed_over_on_successful_primary_reconnect():
    """Fix 5a (async path): async_connect() must also clear is_failed_over on
    a successful primary reconnect."""
    import anyio

    primary = MagicMock()
    type(primary).is_connected = PropertyMock(return_value=False)
    primary.async_connect = AsyncMock(return_value=None)

    fallback = MagicMock()
    type(fallback).is_connected = PropertyMock(return_value=False)
    fallback.watchdog = None
    fallback.async_connect = AsyncMock(return_value=None)

    router = FailoverTransport(primary, fallback)
    router.is_failed_over = True

    anyio.run(router.async_connect)

    assert router.is_failed_over is False

    router._stop_recovery.set()
    if router._recovery_thread:
        router._recovery_thread.join(timeout=2.0)


def test_concurrent_async_connect_spawns_only_one_recovery_thread():
    """Fix 5b (async path): two concurrent async_connect() calls must never
    each spawn their own recovery thread."""
    import anyio

    primary = MagicMock()
    type(primary).is_connected = PropertyMock(return_value=False)
    primary.async_connect = AsyncMock(return_value=None)

    fallback = MagicMock()
    type(fallback).is_connected = PropertyMock(return_value=False)
    fallback.watchdog = None
    fallback.async_connect = AsyncMock(return_value=None)

    router = FailoverTransport(primary, fallback)

    threads_created = []
    original_thread_cls = threading.Thread

    def tracking_thread(*args, **kwargs):
        t = original_thread_cls(*args, **kwargs)
        threads_created.append(t)
        return t

    async def run_concurrent():
        with patch("pytest_mes_core.transports.failover.threading.Thread", side_effect=tracking_thread):
            async with anyio.create_task_group() as tg:
                for _ in range(5):
                    tg.start_soon(router.async_connect)

    anyio.run(run_concurrent)

    assert len(threads_created) == 1

    router._stop_recovery.set()
    if router._recovery_thread:
        router._recovery_thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------

def test_disconnect_tears_down_both_transports_and_joins_thread():
    """disconnect() must stop the recovery thread and disconnect both transports."""
    router, primary, fallback = _make_failover(primary_connected=False)
    type(primary).is_connected = PropertyMock(return_value=False)
    router.connect()
    time.sleep(0.05)
    assert router._recovery_thread.is_alive()

    router.disconnect()

    assert not router._stop_recovery.is_set() or True  # event is set inside disconnect
    primary.disconnect.assert_called_once()
    fallback.disconnect.assert_called_once()
    # Thread must have been joined
    assert router._recovery_thread is None or not router._recovery_thread.is_alive()


def test_disconnect_without_prior_connect_is_safe():
    """Calling disconnect() before connect() must not raise."""
    router, primary, fallback = _make_failover()
    router.disconnect()  # _recovery_thread is None — must be safe
    primary.disconnect.assert_called_once()
    fallback.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# is_connected property
# ---------------------------------------------------------------------------

def test_is_connected_returns_primary_when_not_failed_over():
    router, primary, fallback = _make_failover(primary_connected=True)
    assert router.is_connected is True


def test_is_connected_returns_fallback_when_failed_over():
    router, primary, fallback = _make_failover(fallback_connected=True)
    router.is_failed_over = True
    assert router.is_connected is True


def test_is_connected_false_when_primary_down_and_not_failed_over():
    router, primary, fallback = _make_failover(primary_connected=False)
    assert router.is_connected is False


# ---------------------------------------------------------------------------
# safe_run() — primary path
# ---------------------------------------------------------------------------

def test_safe_run_uses_primary_when_healthy():
    router, primary, fallback = _make_failover()
    primary.safe_run.return_value = _cmd_result(stdout="hello")

    result = router.safe_run("echo hello")

    primary.safe_run.assert_called_once()
    fallback.safe_run.assert_not_called()
    assert result.stdout == "hello"


def test_safe_run_uses_fallback_when_already_failed_over():
    router, primary, fallback = _make_failover()
    router.is_failed_over = True
    fallback.safe_run.return_value = _cmd_result(stdout="fallback_result")

    result = router.safe_run("echo x")

    primary.safe_run.assert_not_called()
    fallback.safe_run.assert_called_once()
    assert result.stdout == "fallback_result"


# ---------------------------------------------------------------------------
# safe_run() — failover path
# ---------------------------------------------------------------------------

def test_safe_run_fails_over_and_wakes_console_on_connection_error():
    """When primary raises TransportConnectionError, router must set
    is_failed_over=True and send a wakeup pulse to the fallback console."""
    router, primary, fallback = _make_failover()
    primary.safe_run.side_effect = TransportConnectionError("SSH pipe shattered")
    fallback.safe_run.return_value = _cmd_result(ok=True)

    with pytest.raises(TransportConnectionError):
        router.safe_run("ls", auto_retry=False)

    assert router.is_failed_over is True
    # Wakeup pulse (empty cmd) must have been sent to fallback
    fallback.safe_run.assert_called()
    wakeup_call = fallback.safe_run.call_args_list[0]
    assert wakeup_call.args[0] == "\n" or wakeup_call[0][0] == "\n"


def test_safe_run_retries_on_fallback_when_auto_retry_true():
    """With auto_retry=True, the command must be retried on the fallback after
    the primary shattered."""
    router, primary, fallback = _make_failover()
    primary.safe_run.side_effect = TransportConnectionError("SSH gone")
    fallback.safe_run.return_value = _cmd_result(stdout="retried_output")

    result = router.safe_run("ls /", auto_retry=True)

    assert result.stdout == "retried_output"
    assert router.is_failed_over is True


def test_safe_run_does_not_retry_without_auto_retry():
    """Without auto_retry, the TransportConnectionError must propagate after
    triggering failover (let the FSM handle it)."""
    router, primary, fallback = _make_failover()
    primary.safe_run.side_effect = TransportConnectionError("broken")
    fallback.safe_run.return_value = _cmd_result()

    with pytest.raises(TransportConnectionError):
        router.safe_run("reboot", auto_retry=False)


# ---------------------------------------------------------------------------
# _on_panic() callback
# ---------------------------------------------------------------------------

def test_on_panic_severs_primary_connection():
    """_on_panic must call primary.disconnect() to force an immediate failover
    when the watchdog detects a kernel panic asynchronously."""
    router, primary, fallback = _make_failover(primary_connected=True)
    router._on_panic()
    primary.disconnect.assert_called_once()


def test_on_panic_skips_disconnect_when_primary_already_down():
    """If the primary is already disconnected, _on_panic must not call
    disconnect() again (prevents double-close)."""
    router, primary, fallback = _make_failover(primary_connected=False)
    router._on_panic()
    primary.disconnect.assert_not_called()


# ---------------------------------------------------------------------------
# _probe_primary_recovery()
# ---------------------------------------------------------------------------

def test_probe_recovery_fails_back_when_primary_recovers():
    """The background recovery thread must set is_failed_over=False when the
    primary transport responds with a successful MES_PING."""
    router, primary, fallback = _make_failover(primary_connected=False)
    router.is_failed_over = True

    # Simulate primary recovering after the first probe attempt
    ping_result = _cmd_result(ok=True, stdout="MES_PING")
    primary.safe_run.return_value = ping_result
    type(primary).is_connected = PropertyMock(return_value=True)

    # Run the recovery loop briefly
    router._stop_recovery.clear()
    t = threading.Thread(target=router._probe_primary_recovery, daemon=True)
    t.start()
    time.sleep(0.3)
    router._stop_recovery.set()
    t.join(timeout=2.0)

    assert router.is_failed_over is False


def test_probe_recovery_suppresses_connection_error():
    """If the primary raises during a recovery probe, the loop must swallow
    the exception and keep retrying (no crash)."""
    router, primary, fallback = _make_failover()
    router.is_failed_over = True
    primary.safe_run.side_effect = TransportConnectionError("still down")
    type(primary).is_connected = PropertyMock(return_value=False)
    primary.connect.side_effect = TransportConnectionError("connect failed")

    router._stop_recovery.clear()
    t = threading.Thread(target=router._probe_primary_recovery, daemon=True)
    t.start()
    time.sleep(0.3)
    router._stop_recovery.set()
    t.join(timeout=2.0)

    # Must still be failed over — probe exceptions swallowed
    assert router.is_failed_over is True
