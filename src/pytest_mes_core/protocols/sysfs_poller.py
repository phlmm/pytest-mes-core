import structlog
import logging
from typing import Dict, List, Any
from pytest_mes_core.transports import DutTransport
from pytest_mes_core.config import SysfsPollerConfig
logger = structlog.get_logger('mes_core.protocols.sysfs')

class BackgroundSysfsPoller:
    """Transport-Agnostic telemetry daemon.
    
    Dynamically applies Host-Side Chunking if running over SSH,
    or standard end-of-test fetching if running over Serial.
    Protected against kernel driver lockups via POSIX timeouts.
    """

    def __init__(self, dut: DutTransport, cfg: SysfsPollerConfig):
        """Initializes the background SysFS polling daemon.

        Args:
            dut: The transport interface connected to the target.
            cfg: The sysfs polling configuration defining targets and intervals.
        """
        self.dut = dut
        self.cfg = cfg
        self.log_file = f'/tmp/mes_sysfs_{id(self)}.log'
        self.pid_file = f'/tmp/mes_sysfs_{id(self)}.pid'
        self._keys = list(self.cfg.targets.keys())
        self._paths = [self.cfg.targets[k].path for k in self._keys]
        self._buffer = None
        self.metrics: Dict[str, Any] = {}

    def __enter__(self) -> 'BackgroundSysfsPoller':
        logger.info('deploying_telemetry_agent_monitoring_keys_interval_polling_interval_s_s', _keys=self._keys, polling_interval_s=self.cfg.polling_interval_s)
        self.dut.safe_run(f'rm -f {self.log_file} {self.pid_file}', hide=True)
        read_timeout = max(0.2, self.cfg.polling_interval_s * 0.5)
        logger.debug('enforcing_read_timeout_s_read_timeout_to_prevent_kernel_driver_lockups', read_timeout=read_timeout)
        cat_commands = ' echo "||" '.join([f'timeout {read_timeout} cat {p} 2>/dev/null' for p in self._paths])
        script = f'while true; do echo $({cat_commands}) >> {self.log_file}; sleep {self.cfg.polling_interval_s}; done'
        logger.debug('[Sysfs] Spawning bash daemon on DUT via nohup...')
        deploy_cmd = f"nohup sh -c '{script}' >/dev/null 2>&1 & echo $! > {self.pid_file}"
        self.dut.safe_run(deploy_cmd, hide=True)
        if type(self.dut).__name__ == 'EphemeralSSHClient':
            logger.debug('[Sysfs] Rich transport detected. Engaging Host-Side Chunking Buffer.')
            from pytest_mes_core.transports import HostSideBuffer
            self._buffer = HostSideBuffer(self.dut, self.log_file, poll_interval_s=1.0)
            self._buffer.start()
        else:
            logger.debug('base_transport_name_detected_falling_back_to_end_of_test_fetching', __name__=type(self.dut).__name__)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Zero-Leakage Teardown & Data Retrieval"""
        logger.debug('[Sysfs] Reaping background daemon and extracting telemetry trace...')
        raw_lines: List[str] = []
        if self._buffer:
            logger.debug('[Sysfs] Extracting vacuumed data from Host PC RAM...')
            raw_lines = self._buffer.stop()
        else:
            logger.debug('extracting_file_log_file_from_dut_over_serial', log_file=self.log_file)
            res = self.dut.safe_run(f'cat {self.log_file}', hide=True, warn=True)
            if res.ok and res.stdout:
                raw_lines = res.stdout.strip().split('\n')
        logger.info('successfully_extracted_val_telemetry_samples_from_dut', val=len(raw_lines))
        logger.debug('zero_leakage_terminating_pid_in_pid_file', pid_file=self.pid_file)
        kill_cmd = f'if [ -f {self.pid_file} ]; then kill -9 $(cat {self.pid_file}) >/dev/null 2>&1 || true; fi'
        self.dut.safe_run(kill_cmd, hide=True)
        self.dut.safe_run(f'rm -f {self.log_file} {self.pid_file}', hide=True)
        self.metrics = self._parse_data(raw_lines)
        if self.metrics:
            logger.debug('aggregated_metrics_metrics', metrics=self.metrics)

    def _parse_data(self, raw_lines: List[str]) -> Dict[str, Any]:
        aggregated: Dict[str, Any] = {}
        if not raw_lines or raw_lines == ['']:
            logger.warning('[Sysfs] No telemetry data captured! Did the kernel panic immediately?')
            return aggregated
        data_matrix: Dict[str, List[str]] = {k: [] for k in self._keys}
        for line in raw_lines:
            vals = [v.strip() for v in line.split('||')]
            if len(vals) != len(self._keys):
                continue
            for key, val_str in zip(self._keys, vals):
                if val_str:
                    data_matrix[key].append(val_str)
        for key, values in data_matrix.items():
            if not values:
                continue
            target_cfg = self.cfg.targets[key]
            unit_suffix = f'_{target_cfg.unit}' if target_cfg.unit else ''
            try:
                num_values = [float(v) * target_cfg.scale for v in values]
                metric_min = min(num_values)
                metric_max = max(num_values)
                metric_avg = round(sum(num_values) / len(num_values), 2)
                aggregated[f'{key}_min{unit_suffix}'] = metric_min
                aggregated[f'{key}_max{unit_suffix}'] = metric_max
                aggregated[f'{key}_avg{unit_suffix}'] = metric_avg
            except ValueError:
                aggregated[f'{key}_final_state'] = values[-1]
        return aggregated