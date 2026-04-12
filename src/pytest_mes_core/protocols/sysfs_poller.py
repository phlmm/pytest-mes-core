import logging
from typing import Dict, List, Any
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.config import SysfsPollerConfig

logger = logging.getLogger("mes_core.protocols.sysfs")

class BackgroundSysfsPoller:
    """
    Generic, stateless, in-band bash daemon.
    Polls arbitrary kernel sysfs nodes during heavy test workloads.
    """
    def __init__(self, dut_ssh: EphemeralSSHClient, cfg: SysfsPollerConfig):
        self.dut = dut_ssh
        self.cfg = cfg
        self.log_file = "/tmp/mes_sysfs_trace.log"
        self.pid_file = "/tmp/mes_sysfs_poller.pid"
        self._keys = list(self.cfg.targets.keys())
        self._paths = list(self.cfg.targets.values())

    def start(self) -> None:
        """Injects and detaches the background monitoring loop on the DUT."""
        logger.info(f"[Sysfs] Deploying generic telemetry agent (Interval: {self.cfg.polling_interval_s}s)...")
        self.dut.safe_run(f"rm -f {self.log_file} {self.pid_file}", hide=True)

        # 1. Construct the bash loop.
        # Using a custom delimiter (||) defends against sysfs files that output spaces.
        cat_commands = ' echo "||" '.join([f"cat {p} 2>/dev/null" for p in self._paths])

        script = (
            f"while true; do "
            f"echo $({cat_commands}) >> {self.log_file}; "
            f"sleep {self.cfg.polling_interval_s}; "
            f"done"
        )

        # 2. Spawn in background via nohup
        deploy_cmd = f"nohup sh -c '{script}' >/dev/null 2>&1 & echo $! > {self.pid_file}"
        self.dut.safe_run(deploy_cmd, hide=True)
        logger.debug("[Sysfs] Background generic daemon armed and detached.")

    def stop_and_aggregate(self) -> Dict[str, Any]:
        """Reaps the daemon, parses the delimited data, and destroys the log."""
        logger.debug("[Sysfs] Reaping background daemon and extracting trace...")

        # 1. Kill the background process surgically
        kill_cmd = f"if [ -f {self.pid_file} ]; then kill -9 $(cat {self.pid_file}) || true; fi"
        self.dut.safe_run(kill_cmd, hide=True)

        # 2. Fetch the raw data
        res = self.dut.safe_run(f"cat {self.log_file}", hide=True, warn=True)
        raw_data = res.stdout.strip().split('\n')

        # 3. ZERO-LEAKAGE Teardown
        self.dut.safe_run(f"rm -f {self.log_file} {self.pid_file}", hide=True)

        return self._parse_data(raw_data)

    def _parse_data(self, raw_lines: List[str]) -> Dict[str, Any]:
        aggregated: Dict[str, Any] = {}
        if not raw_lines or raw_lines == ['']:
            logger.warning("[Sysfs] No telemetry data captured!")
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
            # Construct the final key (e.g., "cpu_freq_max_MHz" instead of just "cpu_freq_max")
            unit_suffix = f"_{target_cfg.unit}" if target_cfg.unit else ""

            try:
                # Apply the scaling factor (e.g., convert 45000 to 45.0)
                num_values = [(float(v) * target_cfg.scale) for v in values]

                # Calculate physics boundaries
                metric_min = min(num_values)
                metric_max = max(num_values)
                metric_avg = round(sum(num_values) / len(num_values), 2)

                aggregated[f"{key}_min{unit_suffix}"] = metric_min
                aggregated[f"{key}_max{unit_suffix}"] = metric_max
                aggregated[f"{key}_avg{unit_suffix}"] = metric_avg

                logger.info(f"[Sysfs] {key} -> Range: [{metric_min}, {metric_max}] {target_cfg.unit}")

            except ValueError:
                # Fallback: String state (e.g., "suspended")
                aggregated[f"{key}_final_state"] = values[-1]
                logger.info(f"[Sysfs] {key} -> Final State: '{values[-1]}'")

        return aggregated
