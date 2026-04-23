"""
PTY-based integration tests for HostPeripheralSerialAdapter.
Covers: connect(), disconnect(), send(), expect(), clear_rx_buffer(),
        context manager, and all error paths (busy port, no such file, generic).
"""
import os
import pty
import time
import threading
import pytest
from unittest.mock import patch, MagicMock
import serial as pyserial

from pytest_mes_core.config import HostSerialConfig
from pytest_mes_core.host_adapters.peripheral_serial import HostPeripheralSerialAdapter
from pytest_mes_core.host_adapters.base import (
    HostAdapterError,
    HostResourceBusyError,
    HostHardwareDisconnectError,
)


def _make_adapter(slave_name: str, timeout_s: float = 1.0) -> HostPeripheralSerialAdapter:
    cfg = HostSerialConfig(port=slave_name, baudrate=115200, timeout_s=timeout_s)
    return HostPeripheralSerialAdapter(cfg)


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------

def test_connect_and_disconnect():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name)
    try:
        adapter.connect()
        assert adapter.ser is not None
        assert adapter.ser.is_open
        adapter.disconnect()
        assert adapter.ser is None
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_connect_is_idempotent():
    """Calling connect() twice must not open a second port handle."""
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name)
    try:
        adapter.connect()
        first_ser = adapter.ser
        adapter.connect()  # second call is a no-op
        assert adapter.ser is first_ser
    finally:
        adapter.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_disconnect_when_not_connected_is_safe():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name)
    # Never connected — should not raise
    adapter.disconnect()
    os.close(master_fd)
    os.close(slave_fd)


def test_disconnect_exception_is_swallowed():
    """If ser.close() raises, disconnect() must swallow it and set ser=None.

    GC-safety note: pyserial's Serial object calls close() in its C-level
    finalizer if the port is still open.  We must ensure that by the time this
    test ends, the Serial instance no longer has an instance-level MagicMock
    for close() — otherwise the GC finalizer will hit the Mock, raise OSError,
    and emit a PytestUnraisableExceptionWarning on a completely unrelated test.

    The safe pattern is:
      1. Close the real port first (so the GC finalizer has nothing to do).
      2. Swap in the Mock to exercise the exception path.
      3. Call disconnect() — must not raise, must set ser=None.
      4. Delete the mock attribute from the object so the original slot is
         restored before any finalizer can run.
    """
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name)
    adapter.connect()

    # Step 1: close the real fd so the OS resource is released immediately.
    # After this the Serial object is in is_open=False state — the finalizer
    # will be a no-op on the underlying fd.
    real_ser = adapter.ser
    real_ser.close()   # actual close — fd released to OS

    # Step 2: inject a poisoned close() as an INSTANCE attribute to exercise
    # the exception-handling path inside disconnect().
    real_ser.close = MagicMock(side_effect=OSError("fd already closed"))

    # Step 3: disconnect() must swallow the OSError and null out self.ser.
    adapter.disconnect()
    assert adapter.ser is None

    # Step 4: scrub the instance-level Mock so the GC finalizer falls through
    # to the class-level no-op (port is already closed, nothing to do).
    try:
        del real_ser.close
    except AttributeError:
        pass  # Already cleaned up

    os.close(master_fd)
    os.close(slave_fd)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

def test_context_manager_connect_and_disconnect():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name)
    try:
        with adapter as ctx:
            assert ctx is adapter
            assert adapter.ser is not None
        assert adapter.ser is None
    finally:
        os.close(master_fd)
        os.close(slave_fd)


# ---------------------------------------------------------------------------
# Error paths in connect()
# ---------------------------------------------------------------------------

def test_connect_no_pyserial_raises():
    """If pyserial is missing, connect() must raise HostAdapterError."""
    cfg = HostSerialConfig(port="/dev/ttyUSB0", baudrate=115200)
    adapter = HostPeripheralSerialAdapter(cfg)

    import pytest_mes_core.host_adapters.peripheral_serial as mod
    original = mod.HAS_SERIAL
    mod.HAS_SERIAL = False
    try:
        with pytest.raises(HostAdapterError, match="pyserial"):
            adapter.connect()
    finally:
        mod.HAS_SERIAL = original


def test_connect_busy_port_with_known_owner_raises_resource_busy():
    cfg = HostSerialConfig(port="/dev/ttyUSB_fake", baudrate=115200)
    adapter = HostPeripheralSerialAdapter(cfg)

    busy_exc = pyserial.SerialException("device or resource busy: '/dev/ttyUSB_fake'")
    with patch("serial.Serial", side_effect=busy_exc), \
         patch("pytest_mes_core.host_adapters.diagnostics.ResourceDiagnostics.get_device_owner",
               return_value="minicom (PID 999)"):
        with pytest.raises(HostResourceBusyError, match="locked by PID"):
            adapter.connect()


def test_connect_busy_port_no_owner_raises_resource_busy():
    cfg = HostSerialConfig(port="/dev/ttyUSB_fake", baudrate=115200)
    adapter = HostPeripheralSerialAdapter(cfg)

    busy_exc = pyserial.SerialException("access is denied")
    with patch("serial.Serial", side_effect=busy_exc), \
         patch("pytest_mes_core.host_adapters.diagnostics.ResourceDiagnostics.get_device_owner",
               return_value=None):
        with pytest.raises(HostResourceBusyError, match="busy"):
            adapter.connect()


def test_connect_missing_port_raises_hardware_disconnect():
    cfg = HostSerialConfig(port="/dev/ttyUSB_gone", baudrate=115200)
    adapter = HostPeripheralSerialAdapter(cfg)

    no_such = pyserial.SerialException("no such file or directory: '/dev/ttyUSB_gone'")
    with patch("serial.Serial", side_effect=no_such):
        with pytest.raises(HostHardwareDisconnectError, match="disconnected"):
            adapter.connect()


def test_connect_generic_hardware_error_raises_adapter_error():
    cfg = HostSerialConfig(port="/dev/ttyUSB_fake", baudrate=115200)
    adapter = HostPeripheralSerialAdapter(cfg)

    generic = pyserial.SerialException("unknown hardware fault")
    with patch("serial.Serial", side_effect=generic):
        with pytest.raises(HostAdapterError, match="Hardware failure"):
            adapter.connect()


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------

def test_send_transmits_payload():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name)
    adapter.connect()
    try:
        adapter.send(b"HELLO_RS485\n")
        time.sleep(0.1)
        data = os.read(master_fd, 64)
        assert b"HELLO_RS485" in data
    finally:
        adapter.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_send_raises_when_not_connected():
    cfg = HostSerialConfig(port="/dev/null", baudrate=115200)
    adapter = HostPeripheralSerialAdapter(cfg)
    with pytest.raises(HostAdapterError, match="not connected"):
        adapter.send(b"anything")


# ---------------------------------------------------------------------------
# clear_rx_buffer()
# ---------------------------------------------------------------------------

def test_clear_rx_buffer_does_not_raise():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name)
    adapter.connect()
    # Put some data in, then clear
    os.write(master_fd, b"stale data\n")
    time.sleep(0.1)
    try:
        adapter.clear_rx_buffer()  # must not raise
    finally:
        adapter.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_clear_rx_buffer_when_disconnected_is_safe():
    cfg = HostSerialConfig(port="/dev/null", baudrate=115200)
    adapter = HostPeripheralSerialAdapter(cfg)
    adapter.clear_rx_buffer()  # ser is None — must be a no-op


# ---------------------------------------------------------------------------
# expect()
# ---------------------------------------------------------------------------

def test_expect_returns_true_when_payload_received():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name, timeout_s=2.0)
    adapter.connect()

    threading.Thread(
        target=lambda: (time.sleep(0.1), os.write(master_fd, b"\x55\xAA\xDE\xAD")),
        daemon=True
    ).start()

    try:
        result = adapter.expect(b"\x55\xAA\xDE\xAD", timeout_s=2.0)
        assert result is True
    finally:
        adapter.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_expect_returns_false_on_timeout():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name, timeout_s=0.5)
    adapter.connect()
    try:
        result = adapter.expect(b"\xFF\xFF\xFF\xFF", timeout_s=0.3)
        assert result is False
    finally:
        adapter.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_expect_returns_false_when_not_connected():
    cfg = HostSerialConfig(port="/dev/null", baudrate=115200)
    adapter = HostPeripheralSerialAdapter(cfg)
    result = adapter.expect(b"anything", timeout_s=0.1)
    assert result is False


def test_expect_finds_payload_within_fragmented_stream():
    """Payload split across multiple reads must still be detected."""
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    adapter = _make_adapter(slave_name, timeout_s=2.0)
    adapter.connect()

    def feed_fragmented():
        time.sleep(0.05)
        os.write(master_fd, b"\xDE\xAD")
        time.sleep(0.1)
        os.write(master_fd, b"\xBE\xEF")

    threading.Thread(target=feed_fragmented, daemon=True).start()

    try:
        result = adapter.expect(b"\xDE\xAD\xBE\xEF", timeout_s=2.0)
        assert result is True
    finally:
        adapter.disconnect()
        os.close(master_fd)
        os.close(slave_fd)
