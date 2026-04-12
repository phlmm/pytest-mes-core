import json
import subprocess
import time
import logging
from tenacity import retry, stop_after_attempt, wait_fixed
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult
from pytest_mes_core.config import EthernetConfig # <-- Import config schema

logger = logging.getLogger("mes_core.protocols.ethernet")

class EthernetValidator:
    @staticmethod
    @retry(stop=stop_after_attempt(5), wait=wait_fixed(1.0), reraise=True)
    def verify_physical_link(dut_ssh: EphemeralSSHClient, cfg: EthernetConfig) -> ValidatorResult:
        logger.info(f"[ETH {cfg.interface}] Polling PHY link state...")

        res_state = dut_ssh.conn.run(f"cat /sys/class/net/{cfg.interface}/operstate", hide=True, warn=True)
        if res_state.stdout.strip().lower() != "up":
            raise RuntimeError(f"Physical link not up on {cfg.interface}")

        res_speed = dut_ssh.conn.run(f"cat /sys/class/net/{cfg.interface}/speed", hide=True, warn=True)
        actual_speed = int(res_speed.stdout.strip())

        if actual_speed < cfg.expected_speed_mbps:
            logger.error(f"[ETH {cfg.interface}] DEGRADED! {actual_speed}Mbps < {cfg.expected_speed_mbps}Mbps.")
            return ValidatorResult(passed=False, metrics={"eth_speed_mbps": float(actual_speed)})

        return ValidatorResult(passed=True, metrics={"eth_speed_mbps": float(actual_speed)})

    @staticmethod
    def measure_throughput(dut_ssh: EphemeralSSHClient, cfg: EthernetConfig) -> ValidatorResult:
        logger.info(f"[ETH] Spawning Host server. Target blasting {cfg.host_iperf_ip} for {cfg.iperf_duration_s}s...")
        server_proc = subprocess.Popen(["iperf3", "-s", "-1", "-J"], stdout=subprocess.PIPE, text=True)
        time.sleep(0.5)

        try:
            cmd = f"iperf3 -c {cfg.host_iperf_ip} -t {cfg.iperf_duration_s} -J"
            result = dut_ssh.conn.run(cmd, hide=True, warn=True)

            if not result.ok:
                return ValidatorResult(passed=False, error_msg="iperf3 client execution failed.")

            data = json.loads(result.stdout)
            mbps = round(data["end"]["sum_sent"]["bits_per_second"] / 1_000_000, 2)
            passed = mbps >= cfg.iperf_min_mbps

            return ValidatorResult(
                passed=passed,
                metrics={"throughput_mbps": mbps},
                error_msg="" if passed else f"Low throughput: {mbps} Mbps"
            )
        finally:
            server_proc.kill()
            server_proc.communicate()
