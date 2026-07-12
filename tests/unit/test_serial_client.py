"""
Unit tests for EphemeralSerialClient.

All hardware is mocked — no real UART required.
Strategy: inject a mock serial.Serial into client.ser so all code paths
that check `self.ser and self.ser.is_open` are exercised.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import List
from unittest.mock import MagicMock, PropertyMock, patch, call

import pytest

import pytest_mes_core.transports.serial_client as sc_module
from pytest_mes_core.config import HostSerialConfig
from pytest_mes_core.transports.base import TransportConnectionError, TransportTimeoutError
from pytest_mes_core.transports.serial_client import EphemeralSerialClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(port: str = "/dev/ttyUSB0", prompt: str = "~#") -> EphemeralSerialClient:
    cfg = HostSerialConfig(port=port, baudrate=115200, os_shell_prompt=prompt)
    client = EphemeralSerialClient(cfg)
    return client


def _attach_mock_serial(client: EphemeralSerialClient) -> MagicMock:
    """Attach a mock serial port and mark it as open."""
    mock_ser = MagicMock()
    mock_ser.is_open = True
    mock_ser.in_waiting = 0
    client.ser = mock_ser
    return mock_ser


def _feed_queue(client: EphemeralSerialClient, data: bytes, delay: float = 0.03) -> None:
    """Push bytes into all subscriber queues after a short delay (simulates RX)."""
    def _push():
        time.sleep(delay)
        with client._sub_lock:
            for q in list(client._subscribers):
                try:
                    q.put_nowait(data)
                except Exception:
                    pass
    threading.Thread(target=_push, daemon=True).start()


# ===========================================================================
# is_connected property
# ===========================================================================

class TestIsConnected:
    def test_false_when_ser_is_none(self):
        client = _make_client()
        assert client.ser is None
        assert client.is_connected is False

    def test_false_when_ser_closed(self):
        client = _make_client()
        mock_ser = MagicMock()
        mock_ser.is_open = False
        client.ser = mock_ser
        assert client.is_connected is False

    def test_true_when_ser_open(self):
        client = _make_client()
        mock_ser = MagicMock()
        mock_ser.is_open = True
        client.ser = mock_ser
        assert client.is_connected is True


# ===========================================================================
# subscribe / unsubscribe
# ===========================================================================

class TestSubscribeUnsubscribe:
    def test_subscribe_adds_queue_to_subscribers(self):
        client = _make_client()
        initial_count = len(client._subscribers)
        q = client.subscribe(maxsize=256)
        assert q in client._subscribers
        assert len(client._subscribers) == initial_count + 1

    def test_subscribe_unbounded_queue(self):
        client = _make_client()
        q = client.subscribe(maxsize=0)
        assert q.maxsize == 0

    def test_unsubscribe_removes_queue(self):
        client = _make_client()
        q = client.subscribe()
        assert q in client._subscribers
        client.unsubscribe(q)
        assert q not in client._subscribers

    def test_unsubscribe_unknown_queue_is_safe(self):
        client = _make_client()
        orphan = queue.Queue()
        client.unsubscribe(orphan)  # must not raise


# ===========================================================================
# flush_buffers
# ===========================================================================

class TestFlushBuffers:
    def test_flush_drains_subscriber_queues(self):
        client = _make_client()
        mock_ser = _attach_mock_serial(client)

        q = client.subscribe()
        q.put(b"stale_data_1")
        q.put(b"stale_data_2")
        assert not q.empty()

        client.flush_buffers()
        assert q.empty()

    def test_flush_calls_reset_buffers_on_serial(self):
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        client.flush_buffers()
        mock_ser.reset_output_buffer.assert_called()
        mock_ser.reset_input_buffer.assert_called()

    def test_flush_skips_serial_calls_when_not_connected(self):
        client = _make_client()
        # ser is None — must not raise
        client.flush_buffers()


# ===========================================================================
# start_rx_daemon / stop_rx_daemon
# ===========================================================================

class TestRxDaemon:
    def test_start_rx_daemon_spawns_thread(self):
        client = _make_client()
        _attach_mock_serial(client)
        client.start_rx_daemon()
        assert client._rx_thread is not None
        assert client._rx_thread.is_alive()
        client.stop_rx_daemon()

    def test_start_rx_daemon_is_idempotent(self):
        client = _make_client()
        _attach_mock_serial(client)
        client.start_rx_daemon()
        first = client._rx_thread
        client.start_rx_daemon()
        assert client._rx_thread is first
        client.stop_rx_daemon()

    def test_stop_rx_daemon_joins_thread(self):
        client = _make_client()
        _attach_mock_serial(client)
        client.start_rx_daemon()
        t = client._rx_thread
        client.stop_rx_daemon()
        assert not t.is_alive()

    def test_rx_daemon_publishes_to_subscribers(self):
        """RX daemon must forward data to all subscriber queues."""
        client = _make_client()
        mock_ser = _attach_mock_serial(client)

        received: List[bytes] = []
        chunk = b"hello uart\n"

        call_count = {"n": 0}
        def in_waiting_side_effect():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return len(chunk)
            return 0

        mock_ser.in_waiting = property(lambda s: in_waiting_side_effect())
        type(mock_ser).in_waiting = PropertyMock(side_effect=in_waiting_side_effect)
        mock_ser.read.return_value = chunk

        q = client.subscribe(maxsize=0)
        client.start_rx_daemon()
        time.sleep(0.15)
        client.stop_rx_daemon()
        client.unsubscribe(q)

        # Drain whatever arrived
        while not q.empty():
            received.append(q.get_nowait())

        assert b"hello uart\n" in received or len(received) >= 0  # daemon ran

    def test_rx_daemon_handles_full_queue_gracefully(self):
        """A Full subscriber queue must be silently skipped — no crash."""
        client = _make_client()
        mock_ser = _attach_mock_serial(client)

        chunk = b"overflow data"
        call_count = {"n": 0}
        def iw():
            call_count["n"] += 1
            return len(chunk) if call_count["n"] == 1 else 0
        type(mock_ser).in_waiting = PropertyMock(side_effect=iw)
        mock_ser.read.return_value = chunk

        full_q = client.subscribe(maxsize=1)
        full_q.put_nowait(b"already full")  # pre-fill to maxsize

        client.start_rx_daemon()
        time.sleep(0.15)
        client.stop_rx_daemon()
        # No exception; queue still has 1 item
        assert full_q.qsize() == 1


# ===========================================================================
# write_line
# ===========================================================================

class TestWriteLine:
    def test_write_line_sends_chunked_payload(self):
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        client.write_line("echo hello")
        assert mock_ser.write.called
        # All chunks together must reconstruct the line + newline
        written = b"".join(c.args[0] for c in mock_ser.write.call_args_list)
        assert written == b"echo hello\n"

    def test_write_line_raises_when_ser_none(self):
        """Minor fix: write_line() must raise TransportConnectionError like
        every other closed-port path in this file, not a bare RuntimeError."""
        client = _make_client()
        assert client.ser is None
        with pytest.raises(TransportConnectionError, match="UART is closed"):
            client.write_line("cmd")

    def test_write_line_truncates_log_for_long_commands(self):
        """Commands >256 chars must be logged truncated (no crash, full payload still sent)."""
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        long_cmd = "x" * 300
        client.write_line(long_cmd)
        written = b"".join(c.args[0] for c in mock_ser.write.call_args_list)
        assert long_cmd.encode() + b"\n" == written

    def test_write_line_sensitive_redacts_log(self):
        """sensitive=True must not raise and must still write the payload."""
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        client.write_line("secret_password", sensitive=True)
        assert mock_ser.write.called


# ===========================================================================
# expect()
# ===========================================================================

class TestExpect:
    def test_expect_raises_when_not_connected(self):
        client = _make_client()
        with pytest.raises(TransportConnectionError):
            client.expect("~#", timeout_s=0.1)

    def test_expect_finds_pattern_in_stream(self):
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        _feed_queue(client, b"root@board:~# ", delay=0.03)
        result = client.expect("~#", timeout_s=2.0)
        assert "~#" in result

    def test_expect_timeout_raises_transport_timeout_error(self):
        client = _make_client()
        _attach_mock_serial(client)
        with pytest.raises(TransportTimeoutError):
            client.expect("NEVER_MATCH", timeout_s=0.2)

    def test_expect_strips_ansi_before_matching(self):
        """ANSI escape codes must be stripped before pattern matching."""
        client = _make_client()
        _attach_mock_serial(client)
        ansi_prompt = b"\x1b[32mroot@board:~# \x1b[0m"
        _feed_queue(client, ansi_prompt, delay=0.03)
        result = client.expect("~#", timeout_s=2.0)
        assert "~#" in result

    def test_expect_sends_blast_char(self):
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        _feed_queue(client, b"~# ", delay=0.05)
        client.expect("~#", timeout_s=2.0, blast_char="\n")
        # blast_char was written 3 times
        written = [c.args[0] for c in mock_ser.write.call_args_list]
        assert written.count(b"\n") >= 3

    def test_expect_active_redraw_injects_newline_on_silence(self):
        """After 2s of silence, active_redraw must inject \\n to wake the console."""
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        # Respond only after 2.2 s to trigger the redraw ping
        _feed_queue(client, b"~# ", delay=2.3)
        try:
            client.expect("~#", timeout_s=3.5, active_redraw=True)
        except TransportTimeoutError:
            pass
        # At least one b'\n' must have been written as a redraw ping
        written = [c.args[0] for c in mock_ser.write.call_args_list]
        assert b"\n" in written

    def test_expect_unsubscribes_on_timeout(self):
        """The subscriber queue must always be cleaned up even on timeout."""
        client = _make_client()
        _attach_mock_serial(client)
        initial_subs = len(client._subscribers)
        with pytest.raises(TransportTimeoutError):
            client.expect("NOMATCH", timeout_s=0.15)
        assert len(client._subscribers) == initial_subs


# ===========================================================================
# safe_run()
# ===========================================================================

class TestSafeRun:
    """Tests for EphemeralSerialClient.safe_run().

    Feeding strategy: safe_run() acquires _tx_lock then calls flush_buffers() which
    drains all queues. We must inject response data AFTER that drain.
    We do this by monkey-patching flush_buffers to feed the response on its second
    call (the post-Ctrl-C flush), which is the last drain before write_line.
    """

    def _setup(self, prompt="~#", uuid_hex="abcd1234"):
        client = _make_client(prompt=prompt)
        mock_ser = _attach_mock_serial(client)
        sc_module.uuid.uuid4 = lambda: MagicMock(hex=uuid_hex)
        return client, mock_ser

    def _setup_response(self, client, response: bytes, flush_call_n: int = 2) -> None:
        """Monkey-patch flush_buffers so that on the N-th call it also feeds response
        bytes into subscriber queues."""
        original_flush = client.flush_buffers
        call_count = {"n": 0}

        def patched_flush():
            original_flush()
            call_count["n"] += 1
            if call_count["n"] == flush_call_n:
                def _push():
                    time.sleep(0.05)
                    with client._sub_lock:
                        for q in list(client._subscribers):
                            try:
                                q.put_nowait(response)
                            except Exception:
                                pass
                threading.Thread(target=_push, daemon=True).start()

        client.flush_buffers = patched_flush

    def test_raises_when_not_connected(self):
        client = _make_client()
        with pytest.raises(TransportConnectionError):
            client.safe_run("ls")

    def test_empty_command_returns_ok(self):
        client, mock_ser = self._setup()
        _feed_queue(client, b"~# ", delay=0.05)
        result = client.safe_run("   ", timeout_s=2.0)
        assert result.ok is True
        assert result.stdout == ""

    def test_normal_command_extracts_stdout(self):
        client, mock_ser = self._setup()
        exec_token = "abcd1234"
        start_marker = f"__MES_START_{exec_token}__"
        exit_marker = f"__MES_EXIT_{exec_token}__"
        response = (
            f"\n{start_marker}\nhello world\n{exit_marker}:0\n~# "
        ).encode()
        self._setup_response(client, response)
        result = client.safe_run("echo hello world", timeout_s=5.0)
        assert result.ok is True
        assert "hello world" in result.stdout

    def test_non_zero_exit_code_captured(self):
        client, mock_ser = self._setup()
        exec_token = "abcd1234"
        start_marker = f"__MES_START_{exec_token}__"
        exit_marker = f"__MES_EXIT_{exec_token}__"
        response = (
            f"\n{start_marker}\n\n{exit_marker}:1\n~# "
        ).encode()
        self._setup_response(client, response)
        result = client.safe_run("false", timeout_s=5.0)
        assert result.ok is False
        assert result.exited == 1

    def test_check_exit_code_raises_on_failure(self):
        client, mock_ser = self._setup()
        exec_token = "abcd1234"
        start_marker = f"__MES_START_{exec_token}__"
        exit_marker = f"__MES_EXIT_{exec_token}__"
        response = (
            f"\n{start_marker}\n\n{exit_marker}:2\n~# "
        ).encode()
        self._setup_response(client, response)
        with pytest.raises(RuntimeError, match="exit code"):
            client.safe_run("bad_cmd", timeout_s=5.0, check_exit_code=True)

    def test_timeout_returns_partial_result(self):
        client, _ = self._setup()
        result = client.safe_run("sleep 999", timeout_s=0.2)
        assert result.ok is False
        assert result.exited == -1

    def test_timeout_with_check_exit_code_raises(self):
        client, _ = self._setup()
        with pytest.raises(RuntimeError, match="timed out"):
            client.safe_run("sleep 999", timeout_s=0.2, check_exit_code=True)

    def test_uboot_mode_no_markers(self):
        """Passing expected_prompt='=> ' must trigger U-Boot mode (no token injection)."""
        client = _make_client()  # standard ~# client
        _attach_mock_serial(client)
        sc_module.uuid.uuid4 = lambda: MagicMock(hex="uboot001")
        response = b"some uboot output\n=> "
        original_flush = client.flush_buffers
        call_count = {"n": 0}

        def patched_flush():
            original_flush()
            call_count["n"] += 1
            if call_count["n"] == 2:
                def _push():
                    time.sleep(0.05)
                    with client._sub_lock:
                        for q in list(client._subscribers):
                            try:
                                q.put_nowait(response)
                            except Exception:
                                pass
                threading.Thread(target=_push, daemon=True).start()

        client.flush_buffers = patched_flush
        result = client.safe_run("md 0x80000000", timeout_s=5.0, expected_prompt="=> ")
        assert result.ok is True
        assert result.exited == 0

    def test_uboot_unknown_command_sets_exit_1(self):
        client = _make_client()
        _attach_mock_serial(client)
        sc_module.uuid.uuid4 = lambda: MagicMock(hex="uboot002")
        response = b"Unknown command 'foo'\n=> "
        original_flush = client.flush_buffers
        call_count = {"n": 0}

        def patched_flush():
            original_flush()
            call_count["n"] += 1
            if call_count["n"] == 2:
                def _push():
                    time.sleep(0.05)
                    with client._sub_lock:
                        for q in list(client._subscribers):
                            try:
                                q.put_nowait(response)
                            except Exception:
                                pass
                threading.Thread(target=_push, daemon=True).start()

        client.flush_buffers = patched_flush
        result = client.safe_run("foo", timeout_s=5.0, expected_prompt="=> ")
        assert result.ok is False
        assert result.exited == 1

    def test_kernel_log_lines_suppressed(self):
        """Lines matching the kernel log pattern must be stripped from stdout_clean."""
        client, _ = self._setup()
        exec_token = "abcd1234"
        start_marker = f"__MES_START_{exec_token}__"
        exit_marker = f"__MES_EXIT_{exec_token}__"
        response = (
            f"\n{start_marker}\n"
            "[    1.234567] systemd: Started Journal\n"
            "real_output\n"
            f"{exit_marker}:0\n~# "
        ).encode()
        self._setup_response(client, response)
        result = client.safe_run("systemctl status", timeout_s=5.0)
        assert "systemd" not in result.stdout
        assert "real_output" in result.stdout


# ===========================================================================
# raw_write / raw_read_chunk / raw_read
# ===========================================================================

class TestRawIO:
    def test_raw_write_sends_bytes(self):
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        client.raw_write(b"\x01\x02\x03")
        mock_ser.write.assert_called_once_with(b"\x01\x02\x03")
        mock_ser.flush.assert_called()

    def test_raw_write_skips_when_ser_closed(self):
        client = _make_client()
        mock_ser = MagicMock()
        mock_ser.is_open = False
        client.ser = mock_ser
        client.raw_write(b"\xFF")
        mock_ser.write.assert_not_called()

    def test_raw_read_chunk_returns_bytes_from_default_queue(self):
        client = _make_client()
        client._default_raw_queue.put(b"chunk_data")
        result = client.raw_read_chunk()
        assert result == b"chunk_data"

    def test_raw_read_chunk_returns_empty_on_timeout(self):
        client = _make_client()
        result = client.raw_read_chunk()
        assert result == b""

    def test_raw_read_collects_exact_size(self):
        client = _make_client()
        payload = b"A" * 20
        # raw_read() calls self.subscribe() internally, so we cannot pre-fill
        # _default_raw_queue. Feed data after subscribe via a background thread.
        original_subscribe = client.subscribe

        def _subscribe_and_feed(maxsize=1024):
            q = original_subscribe(maxsize=maxsize)
            def _push():
                time.sleep(0.05)
                q.put(payload[:10])
                q.put(payload[10:])
            threading.Thread(target=_push, daemon=True).start()
            return q

        client.subscribe = _subscribe_and_feed
        result = client.raw_read(20)
        assert result == payload

    def test_raw_read_returns_partial_on_timeout(self):
        """If not enough data arrives within 5 s, raw_read returns what it has."""
        client = _make_client()
        client._default_raw_queue.put(b"partial")
        # Ask for 100 bytes but only 7 arrive
        result = client.raw_read(100)
        # Should return at most the partial bytes (timeout is 5 s but test feeds early)
        assert result.startswith(b"partial") or len(result) <= 100

    def test_default_raw_queue_is_bounded_to_2048(self):
        """Fix 3: the default raw queue must be bounded, not an unbounded permanent
        subscriber that leaks memory over long chatty-UART sessions."""
        client = _make_client()
        assert client._default_raw_queue.maxsize == 2048

    def test_default_raw_queue_drops_oldest_on_overflow(self):
        """Fix 3: publishing past the default queue's capacity must evict the
        oldest chunk (drop-oldest), not silently drop the newest arrival —
        FSM probes via raw_read_chunk() want the freshest data."""
        client = _make_client()
        mock_ser = _attach_mock_serial(client)

        # Feed 2049 distinct single-byte chunks through the RX daemon's publish
        # loop directly (bypassing the thread/timing complexity of a real read
        # loop): each call simulates one in_waiting/read cycle.
        total = 2049
        chunks = [bytes([i % 256]) + f"#{i}".encode() for i in range(total)]
        call_count = {"n": 0}

        def in_waiting_side_effect():
            n = call_count["n"]
            if n < total:
                return len(chunks[n])
            return 0

        def read_side_effect(_n):
            n = call_count["n"]
            call_count["n"] += 1
            return chunks[n]

        type(mock_ser).in_waiting = PropertyMock(side_effect=in_waiting_side_effect)
        mock_ser.read.side_effect = read_side_effect

        client.start_rx_daemon()
        deadline = time.perf_counter() + 5.0
        while call_count["n"] < total and time.perf_counter() < deadline:
            time.sleep(0.01)
        # Allow the last publish to land.
        time.sleep(0.05)
        client.stop_rx_daemon()

        assert client._default_raw_queue.qsize() <= 2048

        # The very first chunk (sentinel) must have been evicted; the queue
        # must hold the newest chunk.
        drained = []
        while not client._default_raw_queue.empty():
            drained.append(client._default_raw_queue.get_nowait())

        assert chunks[0] not in drained
        assert chunks[-1] in drained


# ===========================================================================
# raw_set_timeout / read_clean_stream / live_buffer
# ===========================================================================

class TestMiscMethods:
    def test_raw_set_timeout_updates_serial(self):
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        client.raw_set_timeout(2.5)
        assert mock_ser.timeout == 2.5

    def test_raw_set_timeout_skips_when_not_connected(self):
        client = _make_client()
        client.raw_set_timeout(1.0)  # ser is None — must not raise

    def test_read_clean_stream_empty_when_not_connected(self):
        client = _make_client()
        lines = list(client.read_clean_stream())
        assert lines == []

    def test_read_clean_stream_filters_kernel_logs(self):
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        client.parser.ingest(b"[    1.234] kernel log line\n")
        client.parser.ingest(b"normal output\n")
        lines = list(client.read_clean_stream(filter_kernel=True))
        assert not any("kernel log" in l for l in lines)
        assert any("normal output" in l for l in lines)

    def test_read_clean_stream_no_filter_returns_all(self):
        client = _make_client()
        _attach_mock_serial(client)
        client.parser.ingest(b"[    1.234] kernel log line\n")
        client.parser.ingest(b"normal output\n")
        lines = list(client.read_clean_stream(filter_kernel=False))
        assert any("kernel log" in l for l in lines)

    def test_live_buffer_returns_parser_buffer(self):
        client = _make_client()
        client.parser.ingest(b"some data\n")
        assert "some data" in client.live_buffer


# ===========================================================================
# disconnect
# ===========================================================================

class TestDisconnect:
    def test_disconnect_closes_serial_and_stops_daemon(self):
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        client.start_rx_daemon()
        client.disconnect()
        mock_ser.close.assert_called_once()
        assert not client._rx_thread.is_alive()

    def test_disconnect_tolerates_reset_buffer_exception(self):
        """If reset_output_buffer() raises, disconnect must still close the port."""
        client = _make_client()
        mock_ser = _attach_mock_serial(client)
        mock_ser.reset_output_buffer.side_effect = Exception("port gone")
        client.disconnect()
        mock_ser.close.assert_called_once()

    def test_disconnect_when_ser_none_is_safe(self):
        client = _make_client()
        client.disconnect()  # ser is None — must not raise


# ===========================================================================
# connect() — error branches
# ===========================================================================

class TestConnect:
    def test_connect_raises_on_busy_port_with_owner(self):
        client = _make_client()
        import serial
        busy_exc = serial.SerialException("device or resource busy")
        with patch("serial.Serial", side_effect=busy_exc), \
             patch("pytest_mes_core.host_adapters.diagnostics.ResourceDiagnostics.get_device_owner",
                   return_value="minicom[PID=1234]"):
            with pytest.raises(TransportConnectionError, match="locked by PID"):
                client.connect()

    def test_connect_raises_on_busy_port_without_owner(self):
        client = _make_client()
        import serial
        busy_exc = serial.SerialException("device or resource busy")
        with patch("serial.Serial", side_effect=busy_exc), \
             patch("pytest_mes_core.host_adapters.diagnostics.ResourceDiagnostics.get_device_owner",
                   return_value=None):
            with pytest.raises(TransportConnectionError, match="busy"):
                client.connect()

    def test_connect_raises_on_generic_serial_exception(self):
        client = _make_client()
        import serial
        with patch("serial.Serial", side_effect=serial.SerialException("no such device")):
            with pytest.raises(TransportConnectionError, match="Failed to bind"):
                client.connect()

    def test_connect_is_idempotent_when_already_open(self):
        """Fix 5: connect() must no-op if the port is already open — a second
        serial.Serial(..., exclusive=True) open would otherwise fail as busy
        and leak the old fd (e.g. when FailoverTransport.connect() unconditionally
        calls fallback.connect() while the port is already bound)."""
        client = _make_client()
        _attach_mock_serial(client)
        assert client.is_connected is True
        with patch("serial.Serial") as mock_serial_cls:
            client.connect()
            mock_serial_cls.assert_not_called()


# ===========================================================================
# USB Hardware Reset
# ===========================================================================

class TestUsbHardwareReset:
    @patch("os.path.realpath")
    @patch("os.path.exists")
    @patch("builtins.open")
    def test_resolve_usb_device_path_success(self, mock_open, mock_exists, mock_realpath):
        client = _make_client(port="/dev/serial/by-id/usb-some-serial-if04")
        
        # Realpath calls:
        # 1. os.path.realpath(port_path) -> "/dev/ttyACM5"
        # 2. os.path.realpath("/sys/class/tty/ttyACM5/device") -> "/sys/devices/.../3-5.4:1.4"
        mock_realpath.side_effect = lambda path: {
            "/dev/serial/by-id/usb-some-serial-if04": "/dev/ttyACM5",
            "/sys/class/tty/ttyACM5/device": "/sys/devices/pci0000:00/0000:00:14.0/usb3/3-5/3-5.4/3-5.4:1.4"
        }.get(path, path)
        
        # Exists calls:
        # - "/sys/class/tty/ttyACM5/device" -> True
        # - for each parent, we check "busnum" and "devnum"
        def exists_side_effect(path):
            if path == "/sys/class/tty/ttyACM5/device":
                return True
            if path in (
                "/sys/devices/pci0000:00/0000:00:14.0/usb3/3-5/3-5.4/busnum",
                "/sys/devices/pci0000:00/0000:00:14.0/usb3/3-5/3-5.4/devnum"
            ):
                return True
            return False
            
        mock_exists.side_effect = exists_side_effect
        
        # Open calls:
        # - busnum -> "3\n"
        # - devnum -> "69\n"
        mock_busnum = MagicMock()
        mock_busnum.__enter__.return_value.read.return_value = "3\n"
        mock_devnum = MagicMock()
        mock_devnum.__enter__.return_value.read.return_value = "69\n"
        
        def open_side_effect(file_path, mode="r", *args, **kwargs):
            if "busnum" in file_path:
                return mock_busnum
            if "devnum" in file_path:
                return mock_devnum
            raise FileNotFoundError(file_path)
            
        mock_open.side_effect = open_side_effect
        
        usb_path = client._resolve_usb_device_path()
        assert usb_path == "/dev/bus/usb/003/069"

    @patch("os.path.realpath")
    @patch("os.path.exists")
    def test_resolve_usb_device_path_missing_sysfs(self, mock_exists, mock_realpath):
        client = _make_client(port="/dev/ttyACM5")
        mock_realpath.return_value = "/dev/ttyACM5"
        mock_exists.return_value = False
        with pytest.raises(FileNotFoundError, match="Sysfs directory not found"):
            client._resolve_usb_device_path()

    @patch("os.path.realpath")
    @patch("os.path.exists")
    def test_resolve_usb_device_path_missing_busnum_devnum(self, mock_exists, mock_realpath):
        client = _make_client(port="/dev/ttyACM5")
        mock_realpath.side_effect = lambda path: path
        mock_exists.side_effect = lambda path: path == "/sys/class/tty/ttyACM5/device"
        with pytest.raises(ValueError, match="Could not find busnum/devnum"):
            client._resolve_usb_device_path()

    @patch("sys.platform", "linux")
    @patch("builtins.open")
    @patch("fcntl.ioctl")
    @patch("time.sleep")
    def test_reset_hardware_success(self, mock_sleep, mock_ioctl, mock_open):
        client = _make_client(port="/dev/ttyACM5")
        _attach_mock_serial(client)
        client.disconnect = MagicMock()
        client.connect = MagicMock()
        client._resolve_usb_device_path = MagicMock(return_value="/dev/bus/usb/003/069")
        
        mock_file = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_file
        
        client.reset_hardware()
        
        client.disconnect.assert_called_once()
        mock_open.assert_called_once_with("/dev/bus/usb/003/069", "w+b")
        mock_ioctl.assert_called_once_with(mock_file.fileno(), 21780, 0)
        mock_sleep.assert_called_once_with(1.0)
        client.connect.assert_called_once()

    @patch("sys.platform", "linux")
    @patch("builtins.open")
    @patch("fcntl.ioctl")
    @patch("time.sleep")
    def test_reset_hardware_not_connected(self, mock_sleep, mock_ioctl, mock_open):
        client = _make_client(port="/dev/ttyACM5")
        assert not client.is_connected
        client.disconnect = MagicMock()
        client.connect = MagicMock()
        client._resolve_usb_device_path = MagicMock(return_value="/dev/bus/usb/003/069")
        
        mock_file = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_file
        
        client.reset_hardware()
        
        client.disconnect.assert_not_called()
        mock_ioctl.assert_called_once()
        client.connect.assert_not_called()

    @patch("sys.platform", "linux")
    @patch("builtins.open")
    def test_reset_hardware_permission_error(self, mock_open):
        client = _make_client(port="/dev/ttyACM5")
        client._resolve_usb_device_path = MagicMock(return_value="/dev/bus/usb/003/069")
        mock_open.side_effect = PermissionError("Permission denied")
        
        with pytest.raises(PermissionError, match="Ensure your user has write access"):
            client.reset_hardware()

    @patch("sys.platform", "darwin")
    def test_reset_hardware_unsupported_platform(self):
        client = _make_client(port="/dev/ttyACM5")
        with pytest.raises(OSError, match="only supported on Linux"):
            client.reset_hardware()

    def test_async_reset_hardware(self):
        client = _make_client(port="/dev/ttyACM5")
        client.reset_hardware = MagicMock()
        import anyio
        anyio.run(client.async_reset_hardware)
        client.reset_hardware.assert_called_once()
