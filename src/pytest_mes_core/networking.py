import re
import time
import threading
import logging
import serial  # type: ignore
from typing import Dict, Optional, Any, List, Union
from tenacity import retry, stop_after_attempt, wait_fixed
from fabric import Connection, Config  # type: ignore

from pytest_mes_core.config import SshTargetConfig, BootProfilerConfig


logger = logging.getLogger("mes_core.networking")

class EphemeralSSHClient:
    """
    The Configurable SSH Lobotomizer.
    Adapts to open debug builds or highly secured production builds via TOML identities.
    """
    def __init__(self, cfg: SshTargetConfig):
        self.cfg = cfg
        self.ip_address = cfg.ip_address

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

        # Dynamically build authentication parameters
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

        self.conn = Connection(
            host=cfg.ip_address,
            user=cfg.user,
            port=cfg.port,
            config=ssh_config,
            connect_kwargs=connect_kwargs
        )

    @retry(stop=stop_after_attempt(10), wait=wait_fixed(2.0), reraise=True)
    def wait_for_sshd(self) -> None:
        """Actively polls the DUT until the OpenSSH daemon binds and accepts authentication."""
        logger.debug(f"[SSH] Polling {self.ip_address}:{self.cfg.port} for sshd...")
        try:
            self.conn.open()
            logger.info(f"[SSH] Successfully authenticated with {self.ip_address} as {self.cfg.user}")
        except Exception as e:
            logger.debug(f"[SSH] Connection refused/auth failed. Retrying... ({e})")
            raise RuntimeError(f"Failed to connect to {self.ip_address}: {e}")

    def is_alive(self) -> bool:
        """Non-blocking check to see if the TCP socket is still breathing."""
        if not self.conn.is_connected:
            return False
        try:
            # Send a silent bash no-op to test the pipe
            res = self.conn.run(":", hide=True, warn=True, timeout=2.0)
            return res.ok
        except Exception:
            return False

    def reconnect(self) -> None:
        """Forces a teardown and rebuilds the SSH tunnel."""
        logger.warning(f"[SSH] Re-establishing dropped connection to {self.ip_address}...")
        self.close()

        # Defeat the boot-up delay if the board was just power-cycled
        self.wait_for_sshd()

    def safe_run(self, cmd: str, timeout_s: float = 30.0, **kwargs: Any) -> Any:
        """
        Defensive execution wrapper with Auto-Healing.
        If the socket dropped due to EMI or a silent reboot, it rebuilds the connection seamlessly.
        """
        kwargs.setdefault('hide', True)
        kwargs.setdefault('warn', True)

        try:
            return self.conn.run(cmd, timeout=timeout_s, **kwargs)
        except Exception as e:
            logger.error(f"[SSH] Socket exception during execution: {e}. Attempting auto-heal...")
            self.reconnect()
            # Retry the command exactly once after healing
            return self.conn.run(cmd, timeout=timeout_s, **kwargs)

    def close(self) -> None:
        if self.conn.is_connected:
            self.conn.close()
            logger.debug(f"[SSH] Severed connection to {self.ip_address}")


class AsyncBootProfiler:
    """Asynchronous Regex-based UART monitor driven entirely by TOML config."""

    def __init__(self, cfg: BootProfilerConfig):
        self.cfg = cfg

        # 1. Normalize the milestones to guarantee strict JSONL keys
        normalized_milestones = self._normalize_milestones(cfg.milestones)

        # 2. Precompile regexes immediately
        self.targets = {name: re.compile(pattern) for name, pattern in normalized_milestones.items()}

        # 3. Pre-fill metrics with -1.0 to enforce strict JSONL schema output
        self.metrics: Dict[str, float] = {name: -1.0 for name in self.targets.keys()}

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _normalize_milestones(self, milestones: Union[Dict[str, str], List[str]]) -> Dict[str, str]:
        """
        Defensive Normalization:
        If the user provides a list of raw strings, this converts them into safe,
        Grafana-compliant metric keys (e.g., "U-Boot SPL" -> "t_u_boot_spl")
        """
        if isinstance(milestones, dict):
            return milestones

        normalized: Dict[str, str] = {}
        for pattern in milestones:
            # Lowercase, replace non-alphanumeric with underscores, strip trailing underscores
            safe_slug = re.sub(r'[^a-z0-9]+', '_', pattern.lower()).strip('_')

            # Enforce 't_' prefix for time-based metrics
            safe_key = f"t_{safe_slug}"
            normalized[safe_key] = pattern

        return normalized

    def start_profiling(self, t_zero_perf: float) -> None:
        self.metrics["t_power_on"] = 0.0
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._listen, args=(t_zero_perf,), daemon=True)
        self._thread.start()
        logger.info(f"[Profiler] Armed on {self.cfg.port} @ {self.cfg.baudrate}bps")

    def _listen(self, t_zero: float) -> None:
        pending = self.targets.copy()

        try:
            with serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=0.1) as ser:
                ser.reset_input_buffer()

                while not self._stop_event.is_set() and pending:
                    line = ser.readline().decode('utf-8', errors='replace').strip()
                    if not line: continue

                    current_time = round(time.perf_counter() - t_zero, 3)

                    hit_keys = []
                    for name, regex in pending.items():
                        if regex.search(line):
                            self.metrics[name] = current_time
                            hit_keys.append(name)
                            logger.debug(f"[Profiler] Milestone '{name}' hit at {current_time}s")

                    for k in hit_keys:
                        del pending[k]

        except serial.SerialException as e:
            logger.error(f"[Profiler] FATAL: UART hardware disconnected: {e}")
            self.metrics["profiler_error"] = -1.0

    def stop_and_fetch(self) -> Dict[str, float]:
        """Returns recorded timestamps using the TOML-configured timeout."""
        logger.debug("[Profiler] Halting UART listener...")
        self._stop_event.set()

        if self._thread:
            self._thread.join(timeout=self.cfg.timeout_s)
            if self._thread.is_alive():
                logger.critical(f"[Profiler] UART Thread refused to die on {self.cfg.port}!")

        missed = [k for k, v in self.metrics.items() if v == -1.0 and k != "profiler_error"]
        if missed:
            logger.warning(f"[Profiler] Missed milestones: {missed}")

        return self.metrics
