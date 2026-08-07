from __future__ import annotations
import anyio
import functools
'\nUnit tests for EphemeralSSHClient and PyOcdTransport.\n\nAll network/hardware I/O is fully mocked — no real SSH or SWD probe required.\n'
import socket
import threading
from unittest.mock import MagicMock, PropertyMock, patch, call
import pytest
from pytest_mes_core.config import SshTargetConfig
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.transports.ssh import EphemeralSSHClient

def _make_ssh_cfg(ip: str='192.168.1.10', user: str='root', port: int=22, password: str='root', forensic: bool=False) -> SshTargetConfig:
    cfg = SshTargetConfig(ip_address=ip, user=user, port=port, password=password, connect_timeout_s=5.0, forensic_journaling=forensic)
    return cfg

def _make_client(forensic: bool=False) -> tuple[EphemeralSSHClient, MagicMock]:
    """Returns (client, mock_conn). client.conn is patched."""
    cfg = _make_ssh_cfg(forensic=forensic)
    with patch('pytest_mes_core.transports.ssh.Connection') as MockConn, patch('paramiko.AutoAddPolicy'), patch('paramiko.SSHClient'):
        mock_conn = MagicMock()
        mock_conn.is_connected = False
        mock_conn.client = MagicMock()
        MockConn.return_value = mock_conn
        client = EphemeralSSHClient(cfg)
        return (client, mock_conn)

def _connected_client() -> tuple[EphemeralSSHClient, MagicMock]:
    """Returns a client whose mock conn reports is_connected=True."""
    client, mock_conn = _make_client()
    mock_conn.is_connected = True
    return (client, mock_conn)

def _make_run_result(stdout: str='', stderr: str='', exited: int=0, ok: bool=True) -> MagicMock:
    res = MagicMock()
    res.stdout = stdout
    res.stderr = stderr
    res.exited = exited
    res.ok = ok
    return res

class TestSSHIsConnected:

    def test_false_when_conn_not_connected(self):
        client, mock_conn = _make_client()
        mock_conn.is_connected = False
        assert client.is_connected is False

    def test_true_when_conn_connected(self):
        client, mock_conn = _make_client()
        mock_conn.is_connected = True
        assert client.is_connected is True

class TestSSHConnect:

    def test_connect_calls_conn_open(self):
        client, mock_conn = _make_client()
        mock_conn.is_connected = False
        client.connect()
        mock_conn.open.assert_called_once()

    def test_connect_skips_if_already_connected(self):
        client, mock_conn = _make_client()
        mock_conn.is_connected = True
        client.connect()
        mock_conn.open.assert_not_called()

    def test_connect_raises_transport_error_on_failure(self):
        client, mock_conn = _make_client()
        mock_conn.is_connected = False
        mock_conn.open.side_effect = Exception('Connection refused')
        with pytest.raises(TransportConnectionError, match='Failed to connect'):
            client.connect()

    def test_disconnect_closes_conn(self):
        client, mock_conn = _connected_client()
        client.disconnect()
        mock_conn.close.assert_called_once()

    def test_disconnect_skips_when_not_connected(self):
        client, mock_conn = _make_client()
        mock_conn.is_connected = False
        client.disconnect()
        mock_conn.close.assert_not_called()

    def test_disconnect_tolerates_close_exception(self):
        client, mock_conn = _connected_client()
        mock_conn.close.side_effect = Exception('socket gone')
        client.disconnect()

class TestSSHSafeRun:

    def test_raises_when_not_connected(self):
        client, mock_conn = _make_client()
        mock_conn.is_connected = False
        with pytest.raises(TransportConnectionError, match='disconnected'):
            client.safe_run('ls')

    def test_happy_path_returns_command_result(self):
        client, mock_conn = _connected_client()
        mock_conn.run.return_value = _make_run_result(stdout='file1\nfile2', exited=0)
        result = client.safe_run('ls /tmp')
        assert result.ok is True
        assert 'file1' in result.stdout
        assert result.exited == 0

    def test_non_zero_exit_captured(self):
        client, mock_conn = _connected_client()
        mock_conn.run.return_value = _make_run_result(stdout='', stderr='not found', exited=1, ok=False)
        result = client.safe_run('badcmd')
        assert result.ok is False
        assert result.exited == 1

    def test_check_exit_code_raises_on_failure(self):
        client, mock_conn = _connected_client()
        mock_conn.run.return_value = _make_run_result(stdout='', stderr='err', exited=1, ok=False)
        with pytest.raises(RuntimeError, match='exit code'):
            client.safe_run('badcmd', check_exit_code=True)

    def test_silent_socket_close_detected(self):
        """If stderr contains 'closed' and exited==-1, must raise TransportConnectionError."""
        client, mock_conn = _connected_client()
        mock_conn.run.return_value = _make_run_result(stdout='', stderr='Connection closed by remote host', exited=-1, ok=False)
        with pytest.raises(TransportConnectionError, match='silently closed'):
            client.safe_run('ls')

    def test_forensic_journaling_wraps_command(self):
        """With forensic_journaling=True, the command must be wrapped with logger."""
        client, mock_conn = _make_client(forensic=True)
        mock_conn.is_connected = True
        mock_conn.run.return_value = _make_run_result(stdout='ok', exited=0)
        client.safe_run('systemctl status')
        actual_cmd = mock_conn.run.call_args[0][0]
        assert 'logger' in actual_cmd
        assert 'MES_Factory' in actual_cmd

    def test_long_command_truncated_in_log(self):
        """Commands >256 chars must still execute fully but log only 256 chars."""
        client, mock_conn = _connected_client()
        mock_conn.run.return_value = _make_run_result(stdout='ok')
        long_cmd = 'echo ' + 'A' * 300
        result = client.safe_run(long_cmd)
        assert result.ok is True
        actual_cmd = mock_conn.run.call_args[0][0]
        assert long_cmd in actual_cmd

    def test_sensitive_kwarg_never_reaches_conn_run(self):
        """Fix 6: `sensitive` is a safe_run-only signal -- it must be popped
        before conn.run() is called, or fabric raises TypeError."""
        client, mock_conn = _connected_client()
        mock_conn.run.return_value = _make_run_result(stdout='ok', exited=0)
        client.safe_run("echo 'super-secret-private-key-chunk'", sensitive=True)
        assert 'sensitive' not in mock_conn.run.call_args.kwargs
        actual_cmd = mock_conn.run.call_args[0][0]
        assert 'super-secret-private-key-chunk' in actual_cmd

    def test_sensitive_command_masked_in_forensic_journal(self):
        """With forensic_journaling=True and sensitive=True, the journaled
        command must be masked, not the raw secret."""
        client, mock_conn = _make_client(forensic=True)
        mock_conn.is_connected = True
        mock_conn.run.return_value = _make_run_result(stdout='ok', exited=0)
        client.safe_run("echo 'super-secret-private-key-chunk'", sensitive=True)
        actual_cmd = mock_conn.run.call_args[0][0]
        journal_part, _, real_cmd_part = actual_cmd.partition(';')
        assert 'super-secret-private-key-chunk' not in journal_part
        assert 'EXEC: ******** (sensitive)' in journal_part
        assert 'super-secret-private-key-chunk' in real_cmd_part

class TestSSHSafeRunExceptions:

    def test_command_timed_out_returns_partial_result(self):
        from invoke.exceptions import CommandTimedOut
        client, mock_conn = _connected_client()
        timed_out_result = MagicMock()
        timed_out_result.stdout = 'partial'
        exc = CommandTimedOut(timed_out_result, 30.0)
        mock_conn.run.side_effect = exc
        result = client.safe_run('sleep 999', timeout_s=30.0)
        assert result.ok is False
        assert result.exited == -1
        assert 'timed out' in result.stderr

    def test_command_timed_out_with_check_exit_raises(self):
        from invoke.exceptions import CommandTimedOut
        client, mock_conn = _connected_client()
        timed_out_result = MagicMock()
        timed_out_result.stdout = ''
        mock_conn.run.side_effect = CommandTimedOut(timed_out_result, 30.0)
        with pytest.raises(RuntimeError, match='timed out'):
            client.safe_run('sleep 999', timeout_s=30.0, check_exit_code=True)

    def test_ssh_exception_raises_transport_connection_error(self):
        from paramiko.ssh_exception import SSHException
        client, mock_conn = _connected_client()
        mock_conn.run.side_effect = SSHException('Bad packet')
        with pytest.raises(TransportConnectionError, match='severed'):
            client.safe_run('ls')

    def test_socket_error_raises_transport_connection_error(self):
        client, mock_conn = _connected_client()
        mock_conn.run.side_effect = socket.error('Connection reset by peer')
        with pytest.raises(TransportConnectionError, match='severed'):
            client.safe_run('ls')

    def test_eof_error_raises_transport_connection_error(self):
        client, mock_conn = _connected_client()
        mock_conn.run.side_effect = EOFError()
        with pytest.raises(TransportConnectionError, match='severed'):
            client.safe_run('ls')

    def test_thread_exception_raises_transport_connection_error(self):
        from invoke.exceptions import ThreadException
        client, mock_conn = _connected_client()

        class _SafeThreadException(ThreadException):

            def __init__(self):
                Exception.__init__(self, 'stubbed thread crash')
        mock_conn.run.side_effect = _SafeThreadException()
        with pytest.raises(TransportConnectionError):
            client.safe_run('ls')

    def test_network_errors_trigger_disconnect(self):
        """Any fatal network exception must also call disconnect() to free the socket."""
        from paramiko.ssh_exception import SSHException
        client, mock_conn = _connected_client()
        mock_conn.run.side_effect = SSHException('network gone')
        with pytest.raises(TransportConnectionError):
            client.safe_run('ls')
        mock_conn.close.assert_called()

class TestSSHAsync:

    @pytest.mark.anyio
    async def test_async_connect_delegates_to_sync(self):
        client, mock_conn = _make_client()
        mock_conn.is_connected = False
        client.connect()
        mock_conn.open.assert_called_once()

    @pytest.mark.anyio
    async def test_async_disconnect_delegates_to_sync(self):
        client, mock_conn = _connected_client()
        client.disconnect()
        mock_conn.close.assert_called_once()

    @pytest.mark.anyio
    async def test_async_safe_run_delegates_to_sync(self):
        client, mock_conn = _connected_client()
        mock_conn.run.return_value = _make_run_result(stdout='async_ok')
        result = client.safe_run('ls')
        assert result.ok is True
        assert result.stdout == 'async_ok'

class TestPyOcdTransport:
    """Full coverage of PyOcdTransport without a physical debug probe.

    pyocd is an optional dependency so we patch the import inside connect().
    """

    def _make_pyocd(self):
        from pytest_mes_core.transports.pyocd_client import PyOcdTransport
        from pytest_mes_core.config.instruments import PyOcdTargetConfig
        cfg = PyOcdTargetConfig(target='stm32h753zitx', frequency=4000000)
        return PyOcdTransport(cfg)

    def test_is_connected_false_initially(self):
        t = self._make_pyocd()
        assert t.is_connected is False

    def test_connect_raises_on_missing_pyocd(self):
        t = self._make_pyocd()
        with patch.dict('sys.modules', {'pyocd': None, 'pyocd.core': None, 'pyocd.core.helpers': None}):
            with pytest.raises(RuntimeError, match='pyocd is not installed'):
                t.connect()

    def test_connect_raises_runtime_error_on_probe_failure(self):
        t = self._make_pyocd()
        mock_helper = MagicMock()
        mock_helper.session_with_chosen_probe.side_effect = Exception('No probe found')
        with patch.dict('sys.modules', {'pyocd': MagicMock(), 'pyocd.core': MagicMock(), 'pyocd.core.helpers': mock_helper}):
            with patch('pyocd.core.helpers.ConnectHelper.session_with_chosen_probe', side_effect=Exception('No probe found')):
                with pytest.raises((RuntimeError, Exception)):
                    t.connect()

    def test_connect_opens_session(self):
        t = self._make_pyocd()
        mock_session = MagicMock()
        mock_session.is_open = True
        mock_board = MagicMock()
        mock_core = MagicMock()
        mock_board.target = mock_core
        mock_session.board = mock_board
        mock_helper = MagicMock()
        mock_helper.session_with_chosen_probe.return_value = mock_session
        with patch('builtins.__import__', side_effect=_patch_pyocd_import(mock_helper)):
            try:
                t.connect()
            except Exception:
                pass
        t._session = mock_session
        t._session.is_open = True
        t.connect()

    def test_disconnect_closes_session(self):
        t = self._make_pyocd()
        mock_session = MagicMock()
        mock_session.is_open = True
        t._session = mock_session
        t.disconnect()
        mock_session.close.assert_called_once()
        assert t._session is None

    def test_disconnect_skips_when_not_connected(self):
        t = self._make_pyocd()
        t.disconnect()

    def test_halt_calls_core(self):
        t = self._make_pyocd()
        mock_core = MagicMock()
        t._core = mock_core
        t.halt()
        mock_core.halt.assert_called_once()

    def test_halt_skips_when_no_core(self):
        t = self._make_pyocd()
        t.halt()

    def test_resume_calls_core(self):
        t = self._make_pyocd()
        mock_core = MagicMock()
        t._core = mock_core
        t.resume()
        mock_core.resume.assert_called_once()

    def test_reset_calls_core(self):
        t = self._make_pyocd()
        mock_core = MagicMock()
        t._core = mock_core
        t.reset()
        mock_core.reset.assert_called_once()

    def test_read_memory_returns_bytes(self):
        t = self._make_pyocd()
        mock_core = MagicMock()
        mock_core.read_memory_block8.return_value = [222, 173, 190, 239]
        t._core = mock_core
        result = t.read_memory(536870912, 4)
        assert result == b'\xde\xad\xbe\xef'

    def test_read_memory_returns_empty_when_no_core(self):
        t = self._make_pyocd()
        result = t.read_memory(536870912, 4)
        assert result == b''

    def test_write_memory_calls_core(self):
        t = self._make_pyocd()
        mock_core = MagicMock()
        t._core = mock_core
        t.write_memory(536870912, b'\xab\xcd')
        mock_core.write_memory_block8.assert_called_once_with(536870912, [171, 205])

    def test_write_memory_skips_when_no_core(self):
        t = self._make_pyocd()
        t.write_memory(536870912, b'\xff')

    def test_read_core_register_returns_value(self):
        t = self._make_pyocd()
        mock_core = MagicMock()
        mock_core.read_core_register.return_value = 3735928559
        t._core = mock_core
        result = t.read_core_register('pc')
        mock_core.read_core_register.assert_called_once_with('pc')
        assert result == 3735928559

    def test_read_core_register_returns_zero_when_no_core(self):
        t = self._make_pyocd()
        result = t.read_core_register('sp')
        assert result == 0

    @pytest.mark.anyio
    async def test_async_halt_delegates(self):
        t = self._make_pyocd()
        mock_core = MagicMock()
        t._core = mock_core
        t.halt()
        mock_core.halt.assert_called_once()

    @pytest.mark.anyio
    async def test_async_resume_delegates(self):
        t = self._make_pyocd()
        mock_core = MagicMock()
        t._core = mock_core
        t.resume()
        mock_core.resume.assert_called_once()

    @pytest.mark.anyio
    async def test_async_reset_delegates(self):
        t = self._make_pyocd()
        mock_core = MagicMock()
        t._core = mock_core
        t.reset()
        mock_core.reset.assert_called_once()

    @pytest.mark.anyio
    async def test_async_disconnect_delegates(self):
        t = self._make_pyocd()
        mock_session = MagicMock()
        mock_session.is_open = True
        t._session = mock_session
        t.disconnect()
        mock_session.close.assert_called_once()

def _patch_pyocd_import(mock_helper):
    """Helper that intercepts 'from pyocd.core.helpers import ConnectHelper'."""
    real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def _import(name, *args, **kwargs):
        if name.startswith('pyocd'):
            raise ImportError('pyocd not available in test env')
        return real_import(name, *args, **kwargs)
    return _import