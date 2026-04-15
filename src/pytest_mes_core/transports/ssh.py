# src/pytest_mes_core/transports/ssh.py
import time
import socket
import logging
from typing import Any, Dict

from tenacity import retry, stop_after_attempt, wait_fixed, before_sleep_log
from fabric import Connection, Config  # type: ignore
from paramiko.ssh_exception import SSHException  # type: ignore
from invoke.exceptions import CommandTimedOut, ThreadException  # type: ignore

from pytest_mes_core.config import SshTargetConfig
from pytest_mes_core.transports.base import (
    CommandResult,
    TransportConnectionError,
    TransportTimeoutError
)

logger = logging.getLogger("mes_core.transports.ssh")

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

        connect_kwargs: Dict[str, Any] = {
            "look_for_keys": False,  # Strict IaC mode: no snooping in ~/.ssh/
            "allow_agent": False,    # Strict IaC mode: no background agents
            "banner_timeout": 5.0,
            "auth_timeout": 5.0,
            "timeout": cfg.connect_timeout_s,
            # Prevents Paramiko from triggering the Dropbear 2022 negotiation crash
            "disabled_algorithms": dict(pubkeys=["rsa-sha2-512", "rsa-sha2-256"])
        }

        if cfg.password:
            connect_kwargs["password"] = cfg.password

        identity_file_path = getattr(cfg, 'identity_file', None)

        if identity_file_path:
            logger.debug(f"[SSH] Loading strict PKI identity: {identity_file_path}")
            # The clean, generic way to pass keys to Fabric/Paramiko
            connect_kwargs["key_filename"] = identity_file_path
        else:
            logger.warning("[SSH] No identity_file defined in TOML! Paramiko will attempt a blank login.")

        conn = Connection(
            host=cfg.ip_address,
            user=cfg.user,
            port=cfg.port,
            connect_kwargs=connect_kwargs
        )

        import paramiko
        conn.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        return conn

    # ==========================================
    # LIFECYCLE MANAGEMENT
    # ==========================================
    @property
    def is_connected(self) -> bool:
        """Required by DutTransport Contract."""
        return self.conn is not None and self.conn.is_connected

    # Wire the retry loop into the logger so operators can see the boot polling
    @retry(
        stop=stop_after_attempt(10),
        wait=wait_fixed(2.0),
        reraise=True,
        before_sleep=before_sleep_log(logger, logging.WARNING)
    )
    def connect(self) -> None:
        """
        Actively polls the DUT until the OpenSSH daemon binds and accepts authentication.
        Raises TransportConnectionError if the boot window expires.
        """
        if self.is_connected:
            return

        logger.debug(f"[SSH] Polling {self.ip_address}:{self.cfg.port} for OpenSSH Daemon...")
        try:
            self.conn.open()
            logger.info(f"[SSH] Successfully authenticated with {self.ip_address} as {self.cfg.user}")
        except Exception as e:
            logger.debug(f"[SSH] Connection refused/auth failed. Retrying... ({e})")
            raise TransportConnectionError(f"Failed to connect to OpenSSH daemon: {e}")

    def disconnect(self) -> None:
        """Zero-Leakage teardown (Required by DutTransport Contract)."""
        if self.is_connected:
            try:
                logger.debug(f"[SSH] ZERO-LEAKAGE: Tearing down TCP socket to {self.ip_address}...")
                self.conn.close()
            except Exception as e:
                logger.debug(f"[SSH] Teardown exception (Safe to ignore): {e}")

    # ==========================================
    # COMMAND EXECUTION
    # ==========================================
    def safe_run(self, cmd: str, timeout_s: float = 30.0, **kwargs: Any) -> CommandResult:
        """
        Synchronous execution mapped to exact Domain Exceptions.
        Logs every command execution directly to the target's systemd journal for forensic auditing.
        Contains ZERO internal auto-healing to allow external Failover architectures to function.
        """
        if not self.is_connected:
            err_msg = "Cannot execute: SSH socket is disconnected."
            logger.critical(f"[SSH] FATAL: {err_msg}")
            raise TransportConnectionError(err_msg)

        kwargs.setdefault('hide', True)
        kwargs.setdefault('warn', True)

        # 1. Forensic Journal Interceptor
        # Escape single quotes so complex commands don't break the logger syntax
        escaped_cmd = cmd.replace("'", "'\\''")
        # Use ';' instead of '&&' to guarantee execution even if the journal daemon is busy
        wrapped_cmd = f"logger -t MES_Factory 'EXEC: {escaped_cmd}' ; {cmd}"

        # Matrix Tracing: Expose the clean shell command to Pytest (not the wrapped one)
        logger.debug(f"[SSH] TX -> {cmd}")
        t0 = time.perf_counter()

        try:
            # 2. Execute via Fabric using the wrapped command
            res = self.conn.run(wrapped_cmd, timeout=timeout_s, **kwargs)
            duration = round(time.perf_counter() - t0, 3)

            # 3. Catch the "Silent Closure" bug inherent to Paramiko
            if not res.ok and ("closed" in str(res.stderr).lower() or res.exited == -1):
                self.disconnect()
                err_msg = "SSH Socket silently closed during execution."
                logger.critical(f"[SSH] FATAL: {err_msg}")
                raise TransportConnectionError(err_msg)

            # Matrix Tracing
            logger.debug(f"[SSH] RX <- Exited {res.exited} in {duration}s")

            # 4. Return the Immutable Contract (Using original 'cmd')
            return CommandResult(
                command=cmd,
                stdout=res.stdout.strip() if res.stdout else "",
                stderr=res.stderr.strip() if res.stderr else "",
                exited=res.exited if res.exited is not None else -1,
                ok=res.ok,
                duration_s=duration
            )

        except CommandTimedOut as e:
            duration = round(time.perf_counter() - t0, 3)
            logger.warning(f"[SSH] Execution timed out after {timeout_s}s: {cmd}")

            return CommandResult(
                command=cmd,
                stdout=e.result.stdout if hasattr(e, 'result') and e.result else "",
                stderr=f"Command timed out after {timeout_s}s",
                exited=-1,
                ok=False,
                duration_s=duration
            )

        except (SSHException, socket.error, EOFError, ThreadException) as e:
            self.disconnect()
            err_msg = f"Physical TCP/SSH link severed during execution of '{cmd}': {e}"
            logger.critical(f"[SSH] FATAL: {err_msg}")
            raise TransportConnectionError(err_msg) from e
