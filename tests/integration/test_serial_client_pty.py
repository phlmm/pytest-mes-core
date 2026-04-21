"""
PTY-based integration tests for EphemeralSerialClient.
Uses a real virtual TTY pair (pty.openpty) so no physical hardware is needed.
Covers: connect/disconnect, expect(), write_line(), safe_run() paths (Linux, U-Boot,
        timeout, check_exit_code, kernel-log filtering, missing markers), raw
        accessors, read_clean_stream, exclusive_raw_access, and connect error paths.
"""
import os
import pty
import time
import threading
import pytest
from unittest.mock import MagicMock, patch
import serial as pyserial

from pytest_mes_core.config.base import StateMachineConfig
from pytest_mes_core.transports.serial_client import EphemeralSerialClient
from pytest_mes_core.transports.base import TransportConnectionError, TransportTimeoutError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(slave_name: str, prompt: str = "root@board:~#") -> StateMachineConfig:
    """Build a StateMachineConfig that points at our PTY slave."""
    cfg = StateMachineConfig(
        port=slave_name,         # StateMachineConfig inherits from BaseHardwareConfig
        os_shell_prompt=prompt,
    )
    # StateMachineConfig doesn't have a 'port' field — inject it dynamically
    object.__setattr__(cfg, "port", slave_name)
    object.__setattr__(cfg, "baudrate", 115200)
    object.__setattr__(cfg, "timeout_s", 1.0)
    return cfg


def _make_client(slave_name: str, prompt: str = "root@board:~#") -> EphemeralSerialClient:
    # Use a MagicMock config — the only thing EphemeralSerialClient needs from cfg
    # during normal operation is: cfg.port, cfg.baudrate, cfg.os_shell_prompt
    cfg = MagicMock()
    cfg.port = slave_name
    cfg.baudrate = 115200
    cfg.os_shell_prompt = prompt
    cfg.timeout_s = 1.0
    return EphemeralSerialClient(cfg)


def _write(master_fd: int, data: bytes, delay: float = 0.05) -> None:
    time.sleep(delay)
    os.write(master_fd, data)


def _pty_pair():
    master_fd, slave_fd = pty.openpty()
    return master_fd, slave_fd, os.ttyname(slave_fd)


# ---------------------------------------------------------------------------
# connect() / disconnect() basics
# ---------------------------------------------------------------------------

def test_connect_disconnect_lifecycle():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    try:
        assert not client.is_connected
        client.connect()
        assert client.is_connected
        assert client.watchdog._thread is not None
        assert client.watchdog._thread.is_alive()
        client.disconnect()
        assert not client.is_connected
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_connect_busy_port_with_known_owner_raises():
    cfg = MagicMock()
    cfg.port = "/dev/ttyUSB_fake"
    cfg.baudrate = 115200
    client = EphemeralSerialClient(cfg)

    busy_exc = pyserial.SerialException("device or resource busy: '/dev/ttyUSB_fake'")
    with patch("serial.Serial", side_effect=busy_exc), \
         patch("pytest_mes_core.host_adapters.diagnostics.ResourceDiagnostics.get_device_owner",
               return_value="minicom (PID 1234)"):
        with pytest.raises(TransportConnectionError, match="locked by PID"):
            client.connect()


def test_connect_busy_port_no_owner_raises():
    cfg = MagicMock()
    cfg.port = "/dev/ttyUSB_fake"
    cfg.baudrate = 115200
    client = EphemeralSerialClient(cfg)

    busy_exc = pyserial.SerialException("device or resource busy")
    with patch("serial.Serial", side_effect=busy_exc), \
         patch("pytest_mes_core.host_adapters.diagnostics.ResourceDiagnostics.get_device_owner",
               return_value=None):
        with pytest.raises(TransportConnectionError, match="busy"):
            client.connect()


def test_connect_generic_serial_error_raises():
    cfg = MagicMock()
    cfg.port = "/dev/ttyUSB_fake"
    cfg.baudrate = 115200
    client = EphemeralSerialClient(cfg)

    with patch("serial.Serial", side_effect=pyserial.SerialException("some other error")):
        with pytest.raises(TransportConnectionError, match="Failed to bind"):
            client.connect()


# ---------------------------------------------------------------------------
# expect()
# ---------------------------------------------------------------------------

def test_expect_raises_when_not_connected():
    cfg = MagicMock()
    cfg.port = "/dev/null"
    cfg.baudrate = 115200
    client = EphemeralSerialClient(cfg)
    with pytest.raises(TransportConnectionError, match="Serial port is closed"):
        client.expect("anything")


def test_expect_raises_when_locked():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    client._is_locked = True
    try:
        with pytest.raises(RuntimeError, match="locked"):
            client.expect("anything")
    finally:
        client._is_locked = False
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_expect_timeout_raises():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    try:
        with pytest.raises(TransportTimeoutError, match="UART Expect Timeout"):
            client.expect("NEVER_GONNA_APPEAR", timeout_s=0.3)
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_expect_blast_char_and_finds_pattern():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    threading.Thread(
        target=_write, args=(master_fd, b"root@board:~# "), kwargs={"delay": 0.15}, daemon=True
    ).start()
    try:
        result = client.expect("root@board:~#", timeout_s=2.0, blast_char="\n")
        assert "root@board" in result
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_expect_finds_pattern_through_ansi_codes():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    ansi_prompt = b"\x1b[32mroot@board:~#\x1b[0m "
    threading.Thread(
        target=_write, args=(master_fd, ansi_prompt), kwargs={"delay": 0.1}, daemon=True
    ).start()
    try:
        result = client.expect("root@board:~#", timeout_s=2.0)
        assert "root@board:~#" in result
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


# ---------------------------------------------------------------------------
# write_line()
# ---------------------------------------------------------------------------

def test_write_line_raises_when_locked():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    client._is_locked = True
    try:
        with pytest.raises(RuntimeError, match="locked or closed"):
            client.write_line("hello")
    finally:
        client._is_locked = False
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_write_line_sensitive_masks_log(caplog):
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    import logging
    with caplog.at_level(logging.DEBUG, logger="mes_core.transports.serial"):
        client.write_line("my_secret_password", sensitive=True)
    assert "my_secret_password" not in caplog.text
    assert "********" in caplog.text
    client.disconnect()
    os.close(master_fd)
    os.close(slave_fd)


def test_write_line_long_cmd_truncated_in_log(caplog):
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    long_cmd = "A" * 300
    import logging
    with caplog.at_level(logging.DEBUG, logger="mes_core.transports.serial"):
        client.write_line(long_cmd)
    assert "..." in caplog.text
    client.disconnect()
    os.close(master_fd)
    os.close(slave_fd)


def test_write_line_sends_bytes_to_port():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    client.write_line("hello_uart")
    time.sleep(0.1)
    data = os.read(master_fd, 64)
    assert b"hello_uart" in data
    client.disconnect()
    os.close(master_fd)
    os.close(slave_fd)


# ---------------------------------------------------------------------------
# safe_run() — various branches
# ---------------------------------------------------------------------------

def test_safe_run_raises_when_not_connected():
    cfg = MagicMock()
    cfg.port = "/dev/null"
    cfg.baudrate = 115200
    cfg.os_shell_prompt = "root@"
    client = EphemeralSerialClient(cfg)
    with pytest.raises(TransportConnectionError):
        client.safe_run("ls")


def test_safe_run_empty_cmd_wakeup_pulse():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name, prompt="root@board:~#")
    client.connect()
    threading.Thread(
        target=_write, args=(master_fd, b"root@board:~# "), kwargs={"delay": 0.05}, daemon=True
    ).start()
    try:
        result = client.safe_run("")
        assert result.ok is True
        assert result.stdout == ""
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_safe_run_uboot_unknown_command_sets_exited_1():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name, prompt="=> ")
    client.connect()

    def feed_uboot():
        time.sleep(0.15)
        os.write(master_fd, b"=> ")            # ctrl+C drain
        time.sleep(0.05)
        os.write(master_fd, b"Unknown command 'badcmd' - try 'help'\r\n=> ")

    threading.Thread(target=feed_uboot, daemon=True).start()
    try:
        result = client.safe_run("badcmd", expected_prompt="=> ")
        assert result.exited == 1
        assert result.ok is False
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_safe_run_uboot_success_sets_exited_0():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name, prompt="=> ")
    client.connect()

    def feed_uboot():
        time.sleep(0.15)
        os.write(master_fd, b"=> ")
        time.sleep(0.05)
        os.write(master_fd, b"printenv\r\nbootargs=console=ttymxc1\r\n=> ")

    threading.Thread(target=feed_uboot, daemon=True).start()
    try:
        result = client.safe_run("printenv", expected_prompt="=> ")
        assert result.ok is True
        assert result.exited == 0
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_safe_run_timeout_returns_failed_result():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name, prompt="root@board:~#")
    client.connect()
    threading.Thread(
        target=_write, args=(master_fd, b"root@board:~# "), kwargs={"delay": 0.05}, daemon=True
    ).start()
    try:
        result = client.safe_run("sleep 999", timeout_s=0.4)
        assert result.ok is False
        assert result.exited == -1
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_safe_run_timeout_with_check_exit_code_raises():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name, prompt="root@board:~#")
    client.connect()
    threading.Thread(
        target=_write, args=(master_fd, b"root@board:~# "), kwargs={"delay": 0.05}, daemon=True
    ).start()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            client.safe_run("sleep 999", timeout_s=0.4, check_exit_code=True)
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def _inject_framed_response(master_fd, token, stdout_lines, exit_code, prompt, delay=0.2):
    """Background helper: feeds ctrl+C drain then full framed response."""
    time.sleep(delay)
    os.write(master_fd, prompt.encode())          # Ctrl+C drain
    time.sleep(0.05)
    body = "\n".join(stdout_lines)
    resp = (
        f"\n__MES_START_{token}__\n"
        f"{body}\n"
        f"__MES_EXIT_{token}__:{exit_code}\n"
        f"{prompt}"
    ).encode()
    os.write(master_fd, resp)


def test_safe_run_check_exit_code_raises_on_failure():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name, prompt="root@board:~#")

    fixed_token = "aabbccdd"
    mock_uuid = MagicMock(); mock_uuid.hex = fixed_token
    client.connect()

    threading.Thread(
        target=_inject_framed_response,
        args=(master_fd, fixed_token, ["error output"], 1, "root@board:~#"),
        daemon=True
    ).start()

    import pytest_mes_core.transports.serial_client as sc_mod
    original = sc_mod.uuid.uuid4
    sc_mod.uuid.uuid4 = lambda: mock_uuid
    try:
        with pytest.raises(RuntimeError, match="failed with exit code"):
            client.safe_run("false", timeout_s=3.0, check_exit_code=True)
    finally:
        sc_mod.uuid.uuid4 = original
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_safe_run_missing_exit_marker_sets_exited_minus2():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name, prompt="root@board:~#")

    fixed_token = "deadbeef"
    mock_uuid = MagicMock(); mock_uuid.hex = fixed_token
    client.connect()

    def feed_no_exit_marker():
        time.sleep(0.2)
        os.write(master_fd, b"root@board:~# ")
        time.sleep(0.05)
        resp = (
            f"\n__MES_START_{fixed_token}__\n"
            f"partial output\n"
            f"root@board:~# "    # NO exit marker
        ).encode()
        os.write(master_fd, resp)

    import pytest_mes_core.transports.serial_client as sc_mod
    original = sc_mod.uuid.uuid4
    sc_mod.uuid.uuid4 = lambda: mock_uuid
    threading.Thread(target=feed_no_exit_marker, daemon=True).start()
    try:
        result = client.safe_run("partial_cmd", timeout_s=3.0)
        assert result.exited == -2
    finally:
        sc_mod.uuid.uuid4 = original
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_safe_run_strips_kernel_dmesg_spam():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name, prompt="root@board:~#")

    fixed_token = "cafebabe"
    mock_uuid = MagicMock(); mock_uuid.hex = fixed_token
    client.connect()

    threading.Thread(
        target=_inject_framed_response,
        args=(master_fd, fixed_token,
              ["[   14.432089] eth0: link up, 1000Mbps", "real_output_line"],
              0, "root@board:~#"),
        daemon=True
    ).start()

    import pytest_mes_core.transports.serial_client as sc_mod
    original = sc_mod.uuid.uuid4
    sc_mod.uuid.uuid4 = lambda: mock_uuid
    try:
        result = client.safe_run("some_cmd", timeout_s=3.0)
        assert result.ok is True
        assert "eth0" not in result.stdout
        assert "real_output_line" in result.stdout
    finally:
        sc_mod.uuid.uuid4 = original
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_safe_run_success_full_path():
    """Full happy-path: command runs, stdout extracted correctly, exited=0."""
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name, prompt="root@board:~#")

    fixed_token = "11223344"
    mock_uuid = MagicMock(); mock_uuid.hex = fixed_token
    client.connect()

    threading.Thread(
        target=_inject_framed_response,
        args=(master_fd, fixed_token, ["Hello MES"], 0, "root@board:~#"),
        daemon=True
    ).start()

    import pytest_mes_core.transports.serial_client as sc_mod
    original = sc_mod.uuid.uuid4
    sc_mod.uuid.uuid4 = lambda: mock_uuid
    try:
        result = client.safe_run("echo Hello MES", timeout_s=3.0)
        assert result.ok is True
        assert result.exited == 0
        assert "Hello MES" in result.stdout
    finally:
        sc_mod.uuid.uuid4 = original
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


# ---------------------------------------------------------------------------
# Raw port accessors
# ---------------------------------------------------------------------------

def test_raw_write_sends_bytes_to_port():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    try:
        client.raw_write(b"HELLO\n")
        time.sleep(0.1)
        data = os.read(master_fd, 64)
        assert b"HELLO" in data
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_raw_read_chunk_retrieves_pending_data():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    try:
        # Use execution_lock so the watchdog thread doesn't race us for the bytes
        with client.execution_lock():
            os.write(master_fd, b"RAWDATA\n")
            time.sleep(0.15)
            assert client.raw_read_pending() > 0
            chunk = client.raw_read_chunk()
        assert b"RAWDATA" in chunk
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_raw_read_chunk_returns_empty_when_no_data():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    try:
        chunk = client.raw_read_chunk()
        assert chunk == b""
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_raw_set_timeout_updates_serial():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    try:
        client.raw_set_timeout(2.5)
        assert client.ser.timeout == 2.5
        client.raw_set_timeout(0)
        assert client.ser.timeout == 0
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


# ---------------------------------------------------------------------------
# read_clean_stream()
# ---------------------------------------------------------------------------

def test_read_clean_stream_returns_empty_when_disconnected():
    cfg = MagicMock()
    cfg.port = "/dev/null"
    cfg.baudrate = 115200
    client = EphemeralSerialClient(cfg)
    assert list(client.read_clean_stream()) == []


def test_read_clean_stream_yields_lines():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    os.write(master_fd, b"Boot step 1\r\nBoot step 2\r\n")
    time.sleep(0.15)
    try:
        lines = list(client.read_clean_stream(filter_kernel=False))
        assert any("Boot step" in l for l in lines)
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_read_clean_stream_filters_kernel_logs():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    os.write(master_fd, b"[   5.123456] mmc0: error -110\r\nUserland line\r\n")
    time.sleep(0.15)
    try:
        lines = list(client.read_clean_stream(filter_kernel=True))
        assert not any("mmc0" in l for l in lines)
        assert any("Userland" in l for l in lines)
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_live_buffer_property():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    os.write(master_fd, b"partial output")
    time.sleep(0.1)
    chunk = client.raw_read_chunk()
    client.parser.ingest(chunk)
    try:
        assert isinstance(client.live_buffer, str)
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


# ---------------------------------------------------------------------------
# exclusive_raw_access()
# ---------------------------------------------------------------------------

def test_exclusive_raw_access_raises_when_not_connected():
    cfg = MagicMock()
    cfg.port = "/dev/null"
    cfg.baudrate = 115200
    client = EphemeralSerialClient(cfg)
    with pytest.raises(TransportConnectionError, match="port is closed"):
        with client.exclusive_raw_access():
            pass


def test_exclusive_raw_access_stops_watchdog_and_restarts():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    original_thread = client.watchdog._thread
    assert original_thread.is_alive()
    try:
        with client.exclusive_raw_access() as raw_ser:
            assert client._is_locked is True
            assert not original_thread.is_alive()
            assert isinstance(raw_ser, pyserial.Serial)
        assert client._is_locked is False
        new_thread = client.watchdog._thread
        assert new_thread is not None
        assert new_thread.is_alive()
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


def test_exclusive_raw_access_flushes_buffers_on_exit():
    master_fd, slave_fd, slave_name = _pty_pair()
    client = _make_client(slave_name)
    client.connect()
    try:
        with client.exclusive_raw_access() as raw_ser:
            os.write(master_fd, b"stale data from flash\n")
            time.sleep(0.1)
        # After context exit, parser buffer should be cleared
        assert client.live_buffer == ""
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)
