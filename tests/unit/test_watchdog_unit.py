"""
Unit tests for UartKernelWatchdog covering all remaining uncovered branches:
- start() when thread already alive (no-op)
- is_panicked() / get_panic_message()
- _monitor_loop: not-connected path
- _monitor_loop: callback that raises
- _monitor_loop: serial port read exception
- All known panic patterns
"""
import time
import threading
import pytest
from unittest.mock import MagicMock, PropertyMock

from pytest_mes_core.transports.watchdog import UartKernelWatchdog


def _make_watchdog(is_connected: bool = True, is_locked: bool = False,
                   is_executing: bool = False) -> tuple:
    """Returns (watchdog, mock_serial_client)."""
    mock_client = MagicMock()
    mock_client.is_connected = is_connected
    mock_client._is_locked = is_locked
    mock_client._is_executing = is_executing
    mock_client.ANSI_ESCAPE_B = __import__('re').compile(rb'\x1b\[[0-9;]*[a-zA-Z]')
    # Default: no bytes waiting
    mock_client.ser = MagicMock()
    mock_client.ser.in_waiting = 0
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
    wd._panic_event.set()
    wd._panic_msg = "Async Kernel Panic detected during idle/background monitoring."
    assert wd.is_panicked() is True
    assert "Panic" in wd.get_panic_message()


# ---------------------------------------------------------------------------
# _monitor_loop: not-connected path
# ---------------------------------------------------------------------------

def test_monitor_loop_skips_when_not_connected():
    """If serial_client.is_connected is False, the loop must sleep and not read."""
    mock_client = MagicMock()
    mock_client.is_connected = False
    mock_client._is_locked = False
    mock_client._is_executing = False

    wd = UartKernelWatchdog(mock_client)
    wd.start()
    time.sleep(0.3)
    wd.stop()

    # ser.read should never have been called since we're not connected
    mock_client.ser.read.assert_not_called()


def test_monitor_loop_skips_when_locked():
    """If _is_locked is True, the monitor must yield without reading."""
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client._is_locked = True
    mock_client._is_executing = False
    mock_client.ser = MagicMock()
    mock_client.ser.in_waiting = 0
    mock_client.ANSI_ESCAPE_B = __import__('re').compile(rb'\x1b\[[0-9;]*[a-zA-Z]')

    wd = UartKernelWatchdog(mock_client)
    wd.start()
    time.sleep(0.3)
    wd.stop()
    # No panic should have been triggered
    assert not wd.is_panicked()


# ---------------------------------------------------------------------------
# _monitor_loop: callback that raises
# ---------------------------------------------------------------------------

def test_panic_callback_exception_is_swallowed():
    """A panic callback that raises must not crash the watchdog thread."""
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client._is_locked = False
    mock_client._is_executing = False
    mock_client.ANSI_ESCAPE_B = __import__('re').compile(rb'\x1b\[[0-9;]*[a-zA-Z]')

    panic_bytes = b"Kernel panic - not syncing: Fatal exception in interrupt"

    mock_ser = MagicMock()
    type(mock_ser).in_waiting = PropertyMock(return_value=len(panic_bytes))
    mock_ser.read.return_value = panic_bytes
    mock_client.ser = mock_ser

    bad_callback = MagicMock(side_effect=RuntimeError("callback exploded"))

    wd = UartKernelWatchdog(mock_client)
    wd.register_panic_callback(bad_callback)
    wd.start()
    time.sleep(0.5)
    wd.stop()

    assert wd.is_panicked()
    bad_callback.assert_called_once()


# ---------------------------------------------------------------------------
# _monitor_loop: serial port read exception
# ---------------------------------------------------------------------------

def test_monitor_loop_handles_serial_read_exception():
    """If ser.read() raises (e.g., port disconnected), the loop must continue."""
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client._is_locked = False
    mock_client._is_executing = False
    mock_client.ANSI_ESCAPE_B = __import__('re').compile(rb'\x1b\[[0-9;]*[a-zA-Z]')

    mock_ser = MagicMock()
    type(mock_ser).in_waiting = PropertyMock(return_value=10)
    mock_ser.read.side_effect = OSError("port disconnected mid-read")
    mock_client.ser = mock_ser

    wd = UartKernelWatchdog(mock_client)
    wd.start()
    time.sleep(0.4)
    wd.stop()

    # Loop should still be alive (thread joins cleanly after stop)
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
    b"mmc0: error -110",
    b"EXT4-fs error (device mmcblk2p2): ext4_validate_block_bitmap:376",
    b"UBIFS error (ubi0:0 pid 123): ubifs_scan_a_node",
    b"HAB Events",
    b"SEC_ERR",
    b"Signature Verification Failed",
]


@pytest.mark.parametrize("payload", PANIC_PAYLOADS)
def test_watchdog_detects_all_panic_patterns(payload: bytes):
    """Each known panic pattern must trigger is_panicked() == True."""
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client._is_locked = False
    mock_client._is_executing = False
    mock_client.ANSI_ESCAPE_B = __import__('re').compile(rb'\x1b\[[0-9;]*[a-zA-Z]')

    mock_ser = MagicMock()
    type(mock_ser).in_waiting = PropertyMock(return_value=len(payload))
    mock_ser.read.return_value = payload
    mock_client.ser = mock_ser

    wd = UartKernelWatchdog(mock_client)
    wd.start()
    time.sleep(0.4)
    wd.stop()

    assert wd.is_panicked(), f"Pattern not detected: {payload!r}"


def test_watchdog_ignores_normal_boot_output():
    """Normal boot messages must NOT trigger a panic."""
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client._is_locked = False
    mock_client._is_executing = False
    mock_client.ANSI_ESCAPE_B = __import__('re').compile(rb'\x1b\[[0-9;]*[a-zA-Z]')

    normal_output = b"[    2.456789] systemd[1]: Started Journal Service\r\n"

    mock_ser = MagicMock()
    type(mock_ser).in_waiting = PropertyMock(return_value=len(normal_output))
    mock_ser.read.side_effect = [normal_output, b"", b"", b""]
    mock_client.ser = mock_ser

    wd = UartKernelWatchdog(mock_client)
    wd.start()
    time.sleep(0.3)
    wd.stop()

    assert not wd.is_panicked()


def test_register_and_fire_multiple_callbacks():
    """Multiple callbacks are all invoked on a panic."""
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client._is_locked = False
    mock_client._is_executing = False
    mock_client.ANSI_ESCAPE_B = __import__('re').compile(rb'\x1b\[[0-9;]*[a-zA-Z]')

    panic_bytes = b"Kernel panic - not syncing: Fatal"
    mock_ser = MagicMock()
    type(mock_ser).in_waiting = PropertyMock(return_value=len(panic_bytes))
    mock_ser.read.return_value = panic_bytes
    mock_client.ser = mock_ser

    cb1 = MagicMock()
    cb2 = MagicMock()

    wd = UartKernelWatchdog(mock_client)
    wd.register_panic_callback(cb1)
    wd.register_panic_callback(cb2)
    wd.start()
    time.sleep(0.4)
    wd.stop()

    assert wd.is_panicked()
    cb1.assert_called_once()
    cb2.assert_called_once()
