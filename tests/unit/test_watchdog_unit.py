import time
import threading
import queue
import pytest
from unittest.mock import MagicMock, PropertyMock

from pytest_mes_core.transports.watchdog import UartKernelWatchdog


def _make_watchdog(is_connected: bool = True) -> tuple:
    """Returns (watchdog, mock_serial_client)."""
    mock_client = MagicMock()
    mock_client.is_connected = is_connected
    # NOTE: UartKernelWatchdog imports ANSI_ESCAPE_B from constants.py at module
    # level, so there is no need (and it has no effect) to set it on the mock
    # client.  The attribute below has been intentionally removed.

    test_queue = queue.Queue()
    mock_client.subscribe.return_value = test_queue
    mock_client.unsubscribe = MagicMock()
    mock_client.test_queue = test_queue

    wd = UartKernelWatchdog(mock_client)
    return wd, mock_client


# ---------------------------------------------------------------------------
# start / stop idempotency
# ---------------------------------------------------------------------------

def test_start_is_idempotent():
    """Calling start() twice must not spawn a second thread."""
    wd, _ = _make_watchdog()
    wd.start()
    first_thread = wd._thread
    assert first_thread is not None and first_thread.is_alive()

    wd.start()  # Second call — should be no-op
    assert wd._thread is first_thread  # Same object, not replaced

    wd.stop()


def test_stop_when_no_thread_is_safe():
    """stop() before start() must not raise."""
    wd, _ = _make_watchdog()
    assert wd._thread is None
    wd.stop()  # Should not raise


def test_stop_joins_thread():
    wd, _ = _make_watchdog()
    wd.start()
    thread = wd._thread
    assert thread.is_alive()
    wd.stop()
    assert not thread.is_alive()
    assert wd._thread is None


# ---------------------------------------------------------------------------
# is_panicked / get_panic_message
# ---------------------------------------------------------------------------

def test_is_panicked_false_initially():
    wd, _ = _make_watchdog()
    assert wd.is_panicked() is False


def test_get_panic_message_empty_initially():
    wd, _ = _make_watchdog()
    assert wd.get_panic_message() == ""


def test_is_panicked_and_message_after_panic_event():
    wd, _ = _make_watchdog()
    wd._panic_event_set = True
    wd._panic_msg = "Async Kernel Panic detected during idle/background monitoring."
    assert wd.is_panicked() is True
    assert "Panic" in wd.get_panic_message()


# ---------------------------------------------------------------------------
# _monitor_loop: not-connected path
# ---------------------------------------------------------------------------

def test_monitor_loop_skips_when_not_connected():
    """If serial_client.is_connected is False, the loop must sleep and not read."""
    wd, mock_client = _make_watchdog(is_connected=False)
    wd.start()
    time.sleep(0.3)
    wd.stop()

    # Should not subscribe because not connected
    mock_client.subscribe.assert_not_called()


# ---------------------------------------------------------------------------
# _monitor_loop: pluggy event dispatch
# ---------------------------------------------------------------------------

def test_panic_event_dispatched_via_pluggy_bus():
    """A panic must be dispatched to the EventBus."""
    wd, mock_client = _make_watchdog()

    panic_bytes = b"Kernel panic - not syncing: Fatal exception in interrupt"
    mock_client.test_queue.put(panic_bytes)

    from pytest_mes_core.events import bus, hookimpl
    class TestListener:
        def __init__(self):
            self.called = False
        @hookimpl
        def on_uart_event(self, event):
            self.called = True

    listener = TestListener()
    bus.register(listener)

    wd.start()
    time.sleep(0.5)
    wd.stop()

    assert wd.is_panicked()
    assert listener.called


# ---------------------------------------------------------------------------
# _monitor_loop: queue exception handling
# ---------------------------------------------------------------------------

def test_monitor_loop_handles_queue_exception():
    """If q.get() raises, the loop must handle it."""
    wd, mock_client = _make_watchdog()

    # We can mock the queue's get method to raise an exception
    mock_queue = MagicMock()
    mock_queue.get.side_effect = Exception("Queue broken")
    mock_client.subscribe.return_value = mock_queue

    wd.start()
    time.sleep(0.4)
    wd.stop()

    assert not wd.is_panicked()


# ---------------------------------------------------------------------------
# All known panic patterns
# ---------------------------------------------------------------------------

PANIC_PAYLOADS = [
    b"Kernel panic - not syncing: VFS: Unable to mount root fs",
    b"Unable to handle kernel paging request at virtual address",
    b"Oops - undefined instruction",
    b"Out of memory: Killed process 1234 (myapp)",
    b"BUG: soft lockup - CPU#0 stuck for 22s",
    b"rcu_preempt detected stalls on CPUs/tasks",
    b"task blocked for more than 120 seconds",
    b"synchronous external abort",
    # MMC I/O errors — any controller index, any negative errno
    b"mmc0: error -110",
    b"mmc1: error -5",
    b"EXT4-fs error (device mmcblk2p2): ext4_validate_block_bitmap:376",
    b"UBIFS error (ubi0:0 pid 123): ubifs_scan_a_node",
    b"HAB Events",
    b"SEC_ERR",
    b"Signature Verification Failed",
]


@pytest.mark.parametrize("payload", PANIC_PAYLOADS)
def test_watchdog_detects_all_panic_patterns(payload: bytes):
    """Each known panic pattern must trigger is_panicked() == True."""
    wd, mock_client = _make_watchdog()
    mock_client.test_queue.put(payload)

    wd.start()
    time.sleep(0.4)
    wd.stop()

    assert wd.is_panicked(), f"Pattern not detected: {payload!r}"


def test_watchdog_ignores_normal_boot_output():
    """Normal boot messages must NOT trigger a panic."""
    wd, mock_client = _make_watchdog()
    normal_output = b"[    2.456789] systemd[1]: Started Journal Service\r\n"
    mock_client.test_queue.put(normal_output)

    wd.start()
    time.sleep(0.3)
    wd.stop()

    assert not wd.is_panicked()



