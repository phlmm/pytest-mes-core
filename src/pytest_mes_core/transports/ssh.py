import structlog
import time
import socket
import logging
from typing import Any, Dict
from tenacity import retry, stop_after_attempt, wait_fixed, before_sleep_log
from fabric import Connection, Config
from paramiko.ssh_exception import SSHException
from invoke.exceptions import CommandTimedOut, ThreadException
from pytest_mes_core.config import SshTargetConfig
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError, TransportTimeoutError
logger = structlog.get_logger('mes_core.transports.ssh')

# Silence noisy external libraries
logging.getLogger("invoke").setLevel(logging.WARNING)
logging.getLogger("fabric").setLevel(logging.WARNING)
logging.getLogger("paramiko").setLevel(logging.WARNING)

class EphemeralSSHClient:
    """
    The Configurable SSH Lobotomizer.
    Adapts to open debug builds or highly secured production builds via TOML identities.
    Strictly adheres to the DutTransport protocol.
    """

    def __init__(self, cfg: SshTargetConfig):
        self.cfg = cfg
        self.ip_address = cfg.ip_address
        self.conn = self._build_connection(cfg)

    def _build_connection(self, cfg: SshTargetConfig) -> Connection:
        """Dynamically constructs the Fabric/Paramiko configuration matrix."""
        connect_kwargs: Dict[str, Any] = {'look_for_keys': False, 'allow_agent': False, 'banner_timeout': 5.0, 'auth_timeout': 5.0, 'timeout': cfg.connect_timeout_s, 'disabled_algorithms': dict(pubkeys=['rsa-sha2-512', 'rsa-sha2-256'])}
        if cfg.password:
            connect_kwargs['password'] = cfg.get_password()
        identity_file_path = getattr(cfg, 'identity_file', None)
        if identity_file_path:
            logger.debug('loading_strict_pki_identity_identity_file_path', identity_file_path=identity_file_path)
            connect_kwargs['key_filename'] = identity_file_path
        else:
            logger.warning('[SSH] No identity_file defined in TOML! Paramiko will attempt a blank login.')
        conn = Connection(host=cfg.ip_address, user=cfg.user, port=cfg.port, connect_kwargs=connect_kwargs)
        import paramiko
        conn.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        return conn

    @property
    def is_connected(self) -> bool:
        """Required by DutTransport Contract."""
        return self.conn is not None and self.conn.is_connected

    @retry(stop=stop_after_attempt(10), wait=wait_fixed(2.0), reraise=True, before_sleep=before_sleep_log(logger, logging.DEBUG))
    def connect(self) -> None:
        """
        Actively polls the DUT until the OpenSSH daemon binds and accepts authentication.
        Raises TransportConnectionError if the boot window expires.
        """
        if self.is_connected:
            return
        logger.debug('polling_ip_address_port_for_openssh_daemon', ip_address=self.ip_address, port=self.cfg.port)
        try:
            self.conn.open()
            logger.info('successfully_authenticated_with_ip_address_as_user', ip_address=self.ip_address, user=self.cfg.user)
        except Exception as e:
            logger.debug('connection_refused_auth_failed_retrying_e', e=e)
            raise TransportConnectionError(f'Failed to connect to OpenSSH daemon: {e}')

    def disconnect(self) -> None:
        """Zero-Leakage teardown (Required by DutTransport Contract)."""
        if self.is_connected:
            try:
                logger.debug('zero_leakage_tearing_down_tcp_socket_to_ip_address', ip_address=self.ip_address)
                self.conn.close()
            except Exception as e:
                logger.debug('teardown_exception_safe_to_ignore_e', e=e)

    async def async_connect(self) -> None:
        """Async variant of connect."""
        import anyio
        await anyio.to_thread.run_sync(self.connect)

    async def async_disconnect(self) -> None:
        """Async variant of disconnect."""
        import anyio
        await anyio.to_thread.run_sync(self.disconnect)

    def safe_run(self, cmd: str, timeout_s: float=30.0, check_exit_code: bool=False, auto_retry: bool=False, **kwargs: Any) -> CommandResult:
        """Synchronous execution mapped to exact Domain Exceptions.

        Logs every command execution directly to the target's systemd journal for forensic auditing.
        Contains ZERO internal auto-healing to allow external Failover architectures to function.

        Args:
            cmd: The shell command to execute.
            timeout_s: Maximum seconds to wait before timing out.
            check_exit_code: If True, raises RuntimeError on non-zero exit code.
            auto_retry: If True, indicates the command is idempotent (handled by Failover router).
            **kwargs: Additional options passed to fabric.Connection.run.

        Returns:
            CommandResult: The parsed, immutable command outcome.

        Raises:
            TransportConnectionError: If the SSH socket is severed or drops silently.
            RuntimeError: If check_exit_code is True and the command fails or times out.
        """
        if not self.is_connected:
            err_msg = 'Cannot execute: SSH socket is disconnected.'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise TransportConnectionError(err_msg)
        kwargs.setdefault('hide', True)
        kwargs.setdefault('warn', True)
        kwargs.setdefault('in_stream', False)
        # MUST pop before self.conn.run(**kwargs) below -- fabric raises TypeError
        # on any kwarg it doesn't recognize.
        sensitive = bool(kwargs.pop('sensitive', False))
        log_cmd = '********' if sensitive else (cmd if len(cmd) < 256 else cmd[:253] + '...')
        escaped_cmd = log_cmd.replace("'", "'\\''")
        if getattr(self.cfg, 'forensic_journaling', False):
            if sensitive:
                wrapped_cmd = f"logger -t MES_Factory 'EXEC: ******** (sensitive)' ; {cmd}"
            else:
                wrapped_cmd = f"logger -t MES_Factory 'EXEC: {escaped_cmd}' ; {cmd}"
        else:
            wrapped_cmd = cmd
        logger.debug('tx_log_cmd', log_cmd=log_cmd)
        t0 = time.perf_counter()
        try:
            res = self.conn.run(wrapped_cmd, timeout=timeout_s, **kwargs)
            duration = round(time.perf_counter() - t0, 3)
            if not res.ok and ('closed' in str(res.stderr).lower() or res.exited == -1):
                self.disconnect()
                err_msg = 'SSH Socket silently closed during execution.'
                logger.critical('fatal_err_msg', err_msg=err_msg)
                raise TransportConnectionError(err_msg)
            logger.debug('rx_exited_exited_in_duration_s', exited=res.exited, duration=duration)
            result = CommandResult(command=cmd, stdout=res.stdout.strip() if res.stdout else '', stderr=res.stderr.strip() if res.stderr else '', exited=res.exited if res.exited is not None else -1, ok=res.ok, duration_s=duration)
            if check_exit_code and (not result.ok):
                raise RuntimeError(f"Command '{log_cmd}' failed with exit code {result.exited}: {result.stderr}")
            return result
        except CommandTimedOut as e:
            duration = round(time.perf_counter() - t0, 3)
            logger.warning('execution_timed_out_after_timeout_s_s_log_cmd', timeout_s=timeout_s, log_cmd=log_cmd)
            result = CommandResult(command=cmd, stdout=e.result.stdout if hasattr(e, 'result') and e.result else '', stderr=f'Command timed out after {timeout_s}s', exited=-1, ok=False, duration_s=duration)
            if check_exit_code:
                raise RuntimeError(f"Command '{log_cmd}' timed out after {timeout_s}s")
            return result
        except (SSHException, socket.error, EOFError, ThreadException) as e:
            self.disconnect()
            err_msg = f"Physical TCP/SSH link severed during execution of '{log_cmd}': {e}"
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise TransportConnectionError(err_msg) from e

    async def async_safe_run(self, cmd: str, timeout_s: float=30.0, check_exit_code: bool=False, auto_retry: bool=False, **kwargs: Any) -> CommandResult:
        """Async variant of safe_run using thread offloading."""
        import anyio
        from functools import partial
        return await anyio.to_thread.run_sync(
            partial(self.safe_run, cmd, timeout_s=timeout_s, check_exit_code=check_exit_code, auto_retry=auto_retry, **kwargs)
        )