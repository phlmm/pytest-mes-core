# src/pytest_mes_core/protocols/ethernet.py
import json
import time
import subprocess
import logging
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.protocols.base import collect_soc_health
from pytest_mes_core.config import EthernetConfig

logger = logging.getLogger("mes_core.protocols.ethernet")

class EthernetValidator:
    """
    Validates physical PHY link states and measures actual DMA/MAC throughput.
    Features deterministic IP assignment, domain exception routing,
    and zombie-proof host server management.
    """

    @classmethod
    def setup_and_verify_link(cls, dut: DutTransport, cfg: EthernetConfig) -> ValidatorResult:
        """
        Takes absolute control of the interface, flushes stale states,
        assigns deterministic IPs, and polls for PHY Carrier lock.
        """
        logger.info(f"[ETH {cfg.interface}] Enforcing deterministic physical state...")
        context_data: Dict[str, Any] = {}

        try:
            # 1. Zero-State Initialization (Kill daemons that fight us)
            logger.debug(f"[ETH {cfg.interface}] Flushing stale IPs and forcing link DOWN...")
            dut.safe_run(f"ip addr flush dev {cfg.interface} >/dev/null 2>&1 || true")
            dut.safe_run(f"ip link set dev {cfg.interface} down", timeout_s=3.0)

            # 2. Enforce MTU and Static IP
            logger.debug(f"[ETH {cfg.interface}] Enforcing MTU {cfg.mtu} and binding IP {cfg.dut_static_ip}...")
            res_mtu = dut.safe_run(f"ip link set dev {cfg.interface} mtu {cfg.mtu}", timeout_s=3.0)
            if not res_mtu.ok:
                err_msg = f"Hardware rejected MTU {cfg.mtu}."
                logger.error(f"[ETH {cfg.interface}] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg)

            dut.safe_run(f"ip addr add {cfg.dut_static_ip} dev {cfg.interface}", timeout_s=3.0)
            dut.safe_run(f"ip link set dev {cfg.interface} up", timeout_s=3.0)

            # 3. Safe Carrier Polling
            logger.debug(f"[ETH {cfg.interface}] Polling MAC/PHY for Carrier Lock (5.0s timeout)...")
            carrier_up = False
            t_end = time.perf_counter() + 5.0
            while time.perf_counter() < t_end:
                res_state = dut.safe_run(f"cat /sys/class/net/{cfg.interface}/operstate", timeout_s=2.0)
                if res_state.ok and "up" in res_state.stdout.lower():
                    carrier_up = True
                    break
                time.sleep(0.5)

            if not carrier_up:
                #  FORENSIC HARDWARE INTERCEPTOR
                dmesg_res = dut.safe_run(f"dmesg | grep -iE '{cfg.interface}|phy|mac' | tail -n 5")
                context_data["kernel_phy_trace"] = dmesg_res.stdout.strip() if dmesg_res.ok else ""

                logger.critical("="*60)
                logger.critical(f"[ETH {cfg.interface}] FATAL: PHY failed to achieve carrier lock!")
                logger.critical("[ETH] Is the Ethernet cable unplugged? Is the PHY oscillator dead?")
                if context_data["kernel_phy_trace"]:
                    logger.critical(f"[ETH] Kernel Trace:\n{context_data['kernel_phy_trace']}")
                logger.critical("="*60)

                return ValidatorResult(passed=False, error_msg="Physical link did not come up.", context=context_data)

            # 4. Verify Auto-Negotiated Speed
            logger.debug(f"[ETH {cfg.interface}] Carrier locked. Interrogating auto-negotiated link speed...")
            res_speed = dut.safe_run(f"cat /sys/class/net/{cfg.interface}/speed", timeout_s=2.0)
            try:
                actual_speed = int(res_speed.stdout.strip())
            except ValueError:
                actual_speed = -1

            context_data["negotiated_mtu"] = cfg.mtu

            if actual_speed < cfg.expected_speed_mbps:
                #  FORENSIC SILICON INTERCEPTOR
                logger.critical("="*60)
                logger.critical(f"[ETH {cfg.interface}] FATAL: DEGRADED SILICON OR BENT RJ45 PINS DETECTED!")
                logger.critical(f"[ETH {cfg.interface}] Negotiated {actual_speed} Mbps. Expected {cfg.expected_speed_mbps} Mbps.")
                logger.critical("="*60)
                return ValidatorResult(
                    passed=False,
                    error_msg=f"Degraded PHY speed: {actual_speed} Mbps",
                    metrics={"eth_speed_mbps": float(actual_speed)},
                    context=context_data
                )

            logger.info(f"[ETH {cfg.interface}] Physical Link Locked securely at {actual_speed} Mbps.")
            return ValidatorResult(passed=True, metrics={"eth_speed_mbps": float(actual_speed)}, context=context_data)

        except TransportTimeoutError:
            logger.critical(f"[ETH {cfg.interface}] FATAL: DUT completely unresponsive during link configuration. Kernel locked?")
            return ValidatorResult(passed=False, error_msg="DUT completely unresponsive during link configuration.")
        except TransportConnectionError as e:
            logger.critical("="*60)
            logger.critical(f"[ETH {cfg.interface}] FATAL: Transport pipe shattered during network reset!")
            logger.critical("[ETH] SUICIDE TRAP: Did you route an Ethernet test over the SSH transport?")
            logger.critical("="*60)
            return ValidatorResult(
                passed=False,
                error_msg=f"Transport pipe shattered during network reset (Routing error?): {e}"
            )


    @classmethod
    def measure_throughput(cls, dut: DutTransport, cfg: EthernetConfig) -> ValidatorResult:
        logger.info(f"[ETH] Measuring DMA/MAC throughput to {cfg.host_iperf_ip} for {cfg.iperf_duration_s}s...")
        context_data: Dict[str, Any] = {}
        server_proc = None

        try:
            # 1. Spawn Host Server Safely
            logger.debug(f"[ETH] Spawning local Host PC iperf3 daemon on port {cfg.iperf_port}...")
            server_cmd = ["iperf3", "-s", "-p", str(cfg.iperf_port), "-1", "-J"]
            server_proc = subprocess.Popen(server_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.5)

            if server_proc.poll() is not None:
                _, stderr = server_proc.communicate()
                logger.critical(f"[ETH] FATAL: Host PC iperf3 server failed to bind! {stderr.strip()}")
                return ValidatorResult(passed=False, error_msg=f"Host iperf3 server failed to bind: {stderr.strip()}")

            # Pre-test thermal baseline
            baseline_health = collect_soc_health(dut)
            if baseline_health:
                context_data["pre_test_health"] = baseline_health

            # 2. Execute Target Client
            cmd = f"iperf3 -c {cfg.host_iperf_ip} -p {cfg.iperf_port} -t {cfg.iperf_duration_s} -J"
            logger.debug(f"[ETH] Commanding DUT to initiate traffic: {cmd}")
            result = dut.safe_run(cmd, timeout_s=cfg.iperf_duration_s + 5.0)

            # Post-test thermal state
            post_health = collect_soc_health(dut)

            # 3. Shielded JSON Parsing
            if not result.ok:
                context_data["iperf_stderr"] = result.stderr.strip()
                logger.error(f"[ETH] Target iperf3 client execution failed: {context_data['iperf_stderr']}")
                return ValidatorResult(passed=False, error_msg="iperf3 client command failed.", context=context_data)

            try:
                data = json.loads(result.stdout)
                mbps = round(data["end"]["sum_sent"]["bits_per_second"] / 1_000_000, 2)
                context_data["cpu_utilization_percent"] = round(data["end"]["cpu_utilization_percent"]["host_total"], 2)
                context_data["retransmits"] = data["end"]["sum_sent"].get("retransmits", 0)

            except (json.JSONDecodeError, KeyError) as e:
                logger.critical(f"[ETH] FATAL: Failed to parse iperf3 JSON matrix: {e}")
                context_data["raw_output"] = result.stdout.strip()[-500:]
                return ValidatorResult(passed=False, error_msg="Malformed iperf3 output.", context=context_data)

            # 4. Evaluate Hardware Physics
            passed = mbps >= cfg.iperf_min_mbps
            if not passed:
                logger.critical("="*60)
                logger.critical(f"[ETH] FATAL: THROUGHPUT FAILED! DMA/MAC is degraded.")
                logger.critical(f"[ETH] Clocked: {mbps} Mbps | Required Limit: {cfg.iperf_min_mbps} Mbps")
                logger.critical("="*60)

            metrics = {"throughput_mbps": mbps, "eth_retransmits": context_data["retransmits"]}
            metrics.update(post_health)

            return ValidatorResult(
                passed=passed,
                metrics=metrics,
                error_msg="" if passed else f"Low throughput: {mbps} Mbps",
                context=context_data
            )

        except TransportTimeoutError:
            logger.critical("[ETH] FATAL: iperf3 execution caused a hard CPU lockup on the DUT. Check power rails.")
            return ValidatorResult(passed=False, error_msg="iperf3 execution caused a hard CPU lockup on the DUT.")
        except TransportConnectionError as e:
            logger.critical(f"[ETH] FATAL: Transport dropped during iperf. (SSH Suicide Trap?): {e}")
            return ValidatorResult(
                passed=False,
                error_msg=f"Transport dropped during iperf. (Did you route a destructive net test over SSH instead of Serial?): {e}"
            )
        except Exception as e:
            # Fallback for unexpected Python/OS issues on the Host PC side
            logger.critical(f"[ETH] FATAL: Catastrophic Host PC failure during throughput test: {e}")
            return ValidatorResult(passed=False, error_msg=f"Host Execution interrupted: {e}")

        finally:
            # 5. ZERO-LEAKAGE: Host Subprocess Teardown
            if server_proc and server_proc.poll() is None:
                logger.debug("[ETH] ZERO-LEAKAGE: Reaping Host PC iperf3 server daemon...")
                server_proc.kill()
                server_proc.communicate()
