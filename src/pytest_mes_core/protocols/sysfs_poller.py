# src/pytest_mes_core/protocols/sysfs_poller.py
import logging
from typing import Dict, List, Any

from pytest_mes_core.transports import DutTransport
from pytest_mes_core.config import SysfsPollerConfig

logger = logging.getLogger("mes_core.protocols.sysfs")

class BackgroundSysfsPoller:
    """
    Transport-Agnostic telemetry daemon.
    Dynamically applies Host-Side Chunking if running over SSH,
    or standard end-of-test fetching if running over Serial.
    Protected against kernel driver lockups via POSIX timeouts.
    """
    def __init__(self, dut: DutTransport, cfg: SysfsPollerConfig):
        self.dut = dut
        self.cfg = cfg
        self.log_file = f"/tmp/mes_sysfs_{id(self)}.log"
        self.pid_file = f"/tmp/mes_sysfs_{id(self)}.pid"
        self._keys = list(self.cfg.targets.keys())
        # Safely extract the path strings from the Pydantic target models
        self._paths = [self.cfg.targets[k].path for k in self._keys]
        self._buffer = None
        self.metrics: Dict[str, Any] = {}

    def __enter__(self) -> 'BackgroundSysfsPoller':
        logger.info(f"[Sysfs] Deploying telemetry agent monitoring {self._keys} (Interval: {self.cfg.polling_interval_s}s)...")
        self.dut.safe_run(f"rm -f {self.log_file} {self.pid_file}", hide=True)

        # 1. Calculate safe hardware read timeout (Max 50% of the polling interval)
        read_timeout = max(0.2, self.cfg.polling_interval_s * 0.5)
        logger.debug(f"[Sysfs] Enforcing {read_timeout}s read timeout to prevent kernel driver lockups.")

        # 2. Construct the robust, sandboxed bash loop
        cat_commands = ' echo "||" '.join([
            f"timeout {read_timeout} cat {p} 2>/dev/null"
            for p in self._paths
        ])

        script = (
            f"while true; do "
            f"echo $({cat_commands}) >> {self.log_file}; "
            f"sleep {self.cfg.polling_interval_s}; "
            f"done"
        )

        # 3. Spawn in background via nohup
        logger.debug("[Sysfs] Spawning bash daemon on DUT via nohup...")
        deploy_cmd = f"nohup sh -c '{script}' >/dev/null 2>&1 & echo $! > {self.pid_file}"
        self.dut.safe_run(deploy_cmd, hide=True)

        # 4. Dynamic Transport Optimization
        # If we have a rich SSH client, multiplex a background vacuum thread.
        # If we are on a dumb Serial UART, we wait until the end.
        if type(self.dut).__name__ == "EphemeralSSHClient":
            logger.debug("[Sysfs] Rich transport detected. Engaging Host-Side Chunking Buffer.")
            # Local import to avoid circular dependencies if networking.py imports protocols
            from pytest_mes_core.transports import HostSideBuffer
            self._buffer = HostSideBuffer(self.dut, self.log_file, poll_interval_s=1.0)
            self._buffer.start()
        else:
            logger.debug(f"[Sysfs] Base transport ({type(self.dut).__name__}) detected. Falling back to End-of-Test Fetching.")

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Zero-Leakage Teardown & Data Retrieval"""
        logger.debug("[Sysfs] Reaping background daemon and extracting telemetry trace...")
        raw_lines: List[str] = []

        # 1. Fetch Data
        if self._buffer:
            logger.debug("[Sysfs] Extracting vacuumed data from Host PC RAM...")
            # Safely halt the background thread and get the surviving SSH data
            raw_lines = self._buffer.stop()
        else:
            logger.debug(f"[Sysfs] Extracting file {self.log_file} from DUT over Serial...")
            # Serial fallback: Fetch it all now
            res = self.dut.safe_run(f"cat {self.log_file}", hide=True, warn=True)
            if res.ok and res.stdout:
                raw_lines = res.stdout.strip().split('\n')

        logger.info(f"[Sysfs] Successfully extracted {len(raw_lines)} telemetry samples from DUT.")

        # 2. Surgical Kill
        logger.debug(f"[Sysfs] ZERO-LEAKAGE: Terminating PID in {self.pid_file}...")
        kill_cmd = f"if [ -f {self.pid_file} ]; then kill -9 $(cat {self.pid_file}) >/dev/null 2>&1 || true; fi"
        self.dut.safe_run(kill_cmd, hide=True)

        # 3. Clean up RAM disk
        self.dut.safe_run(f"rm -f {self.log_file} {self.pid_file}", hide=True)

        # 4. Parse and attach physics data to the class instance
        self.metrics = self._parse_data(raw_lines)

        if self.metrics:
            logger.debug(f"[Sysfs] Aggregated Metrics: {self.metrics}")

    def _parse_data(self, raw_lines: List[str]) -> Dict[str, Any]:
        aggregated: Dict[str, Any] = {}
        if not raw_lines or raw_lines == ['']:
            logger.warning("[Sysfs] No telemetry data captured! Did the kernel panic immediately?")
            return aggregated

        # Setup tracking arrays
        data_matrix: Dict[str, List[str]] = {k: [] for k in self._keys}

        for line in raw_lines:
            vals = [v.strip() for v in line.split("||")]
            if len(vals) != len(self._keys):
                continue

            for key, val_str in zip(self._keys, vals):
                if val_str:
                    data_matrix[key].append(val_str)

        # Intelligently aggregate and SCALE based on data type
        for key, values in data_matrix.items():
            if not values:
                continue

            target_cfg = self.cfg.targets[key]
            # Construct the final key (e.g., "cpu_freq_max_MHz")
            unit_suffix = f"_{target_cfg.unit}" if target_cfg.unit else ""

            try:
                # Apply the scaling factor (e.g., convert 45000 to 45.0)
                num_values = [(float(v) * target_cfg.scale) for v in values]

                metric_min = min(num_values)
                metric_max = max(num_values)
                metric_avg = round(sum(num_values) / len(num_values), 2)

                aggregated[f"{key}_min{unit_suffix}"] = metric_min
                aggregated[f"{key}_max{unit_suffix}"] = metric_max
                aggregated[f"{key}_avg{unit_suffix}"] = metric_avg

            except ValueError:
                # Fallback: String state (e.g., "suspended")
                aggregated[f"{key}_final_state"] = values[-1]

        return aggregated
