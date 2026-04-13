# src/pytest_mes_core/transports/ssh.py
import time
import socket
import logging
from typing import Any, Dict

from tenacity import retry, stop_after_attempt, wait_fixed
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
        ssh_config = Config(overrides={
            'ssh': {
                'config': {
                    'StrictHostKeyChecking': 'no',
                    'UserKnownHostsFile': '/dev/null',
                    'LogLevel': 'ERROR',
                    'ConnectTimeout': str(int(cfg.connect_timeout_s)),
                    'ServerAliveInterval': '10',
                    'ServerAliveCountMax': '3'
                }
            }
        })

        connect_kwargs: Dict[str, Any] = {
            "look_for_keys": False,
            "allow_agent": False,
            "banner_timeout": 5.0,
            "auth_timeout": 5.0
        }

        if cfg.password:
            connect_kwargs["password"] = cfg.password

        if cfg.identity_file:
            logger.debug(f"[SSH] Loading strict PKI identity from {cfg.identity_file}")
            connect_kwargs["key_filename"] = cfg.identity_file

        return Connection(
            host=cfg.ip_address,
            user=cfg.user,
            port=cfg.port,
            config=ssh_config,
            connect_kwargs=connect_kwargs
        )

    # ==========================================
    # LIFECYCLE MANAGEMENT
    # ==========================================
    @property
    def is_connected(self) -> bool:
        """Required by DutTransport Contract."""
        return self.conn is not None and self.conn.is_connected

    @retry(stop=stop_after_attempt(10), wait=wait_fixed(2.0), reraise=True)
    def connect(self) -> None:
        """
        Actively polls the DUT until the OpenSSH daemon binds and accepts authentication.
        Raises TransportConnectionError if the boot window expires.
        """
        if self.is_connected:
            return

        logger.debug(f"[SSH] Polling {self.ip_address}:{self.cfg.port} for sshd...")
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
                self.conn.close()
                logger.debug(f"[SSH] Severed connection to {self.ip_address}")
            except Exception:
                pass

    # ==========================================
    # COMMAND EXECUTION
    # ==========================================
    def safe_run(self, cmd: str, timeout_s: float = 30.0, **kwargs: Any) -> CommandResult:
        """
        Synchronous execution mapped to exact Domain Exceptions.
        Contains ZERO internal auto-healing to allow external Failover architectures to function.
        """
        if not self.is_connected:
            raise TransportConnectionError("Cannot execute: SSH socket is disconnected.")

        kwargs.setdefault('hide', True)
        kwargs.setdefault('warn', True)

        t0 = time.perf_counter()
        try:
            # 1. Execute via Fabric
            res = self.conn.run(cmd, timeout=timeout_s, **kwargs)
            duration = round(time.perf_counter() - t0, 3)

            # 2. Catch the "Silent Closure" bug inherent to Paramiko
            if not res.ok and "closed" in str(res.stderr).lower():
                self.disconnect()
                raise TransportConnectionError("SSH Socket silently closed during execution.")

            # 3. Return the Immutable Contract
            return CommandResult(
                command=cmd,
                stdout=res.stdout.strip() if res.stdout else "",
                stderr=res.stderr.strip() if res.stderr else "",
                exited=res.exited if res.exited is not None else -1,
                ok=res.ok,
                duration_s=duration
            )

        except CommandTimedOut as e:
            # Application Hang: Command took longer than timeout_s. The pipe is fine.
            raise TransportTimeoutError(f"SSH Command timed out after {timeout_s}s: {e}")

        except (SSHException, socket.error, EOFError, ThreadException) as e:
            # THE SURVIVAL EVENT: The physical pipe shattered.
            self.disconnect()
            raise TransportConnectionError(f"SSH transport severed during execution: {e}")
