import structlog
import json
import time
import anyio
import subprocess
import logging
from typing import Dict, Any
from pytest_mes_core.transports import DutTransport, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.protocols.base import collect_soc_health
from pytest_mes_core.config import EthernetConfig
logger = structlog.get_logger('mes_core.protocols.ethernet')

class EthernetValidator:
    """
    Validates physical PHY link states and measures actual DMA/MAC throughput.
    Features deterministic IP assignment, domain exception routing,
    and zombie-proof host server management.
    """

    @classmethod
    def setup_and_verify_link(cls, dut: DutTransport, cfg: EthernetConfig) -> ValidatorResult:
        """Takes absolute control of the interface, flushes stale states, assigns deterministic IPs, and polls for PHY Carrier lock.

        Args:
            dut: The transport interface connected to the Device Under Test.
            cfg: The Ethernet configuration parameters.

        Returns:
            ValidatorResult: An object containing the validation outcome (passed/failed), 
                captured metrics like negotiated speed, and contextual error information.
        """
        logger.info('enforcing_deterministic_physical_state', interface=cfg.interface)
        context_data: Dict[str, Any] = {}
        try:
            logger.debug('flushing_stale_ips_and_forcing_link_down', interface=cfg.interface)
            dut.safe_run(f'ip addr flush dev {cfg.interface} >/dev/null 2>&1 || true')
            dut.safe_run(f'ip link set dev {cfg.interface} down', timeout_s=3.0)
            logger.debug('enforcing_mtu_mtu_and_binding_ip_dut_static_ip', interface=cfg.interface, mtu=cfg.mtu, dut_static_ip=cfg.dut_static_ip)
            res_mtu = dut.safe_run(f'ip link set dev {cfg.interface} mtu {cfg.mtu}', timeout_s=3.0)
            if not res_mtu.ok:
                err_msg = f'Hardware rejected MTU {cfg.mtu}.'
                logger.error('err_msg', interface=cfg.interface, err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg)
            dut.safe_run(f'ip addr add {cfg.dut_static_ip} dev {cfg.interface}', timeout_s=3.0)
            dut.safe_run(f'ip link set dev {cfg.interface} up', timeout_s=3.0)
            logger.debug('polling_mac_phy_for_carrier_lock_5_0s_timeout', interface=cfg.interface)
            carrier_up = False
            t_end = time.perf_counter() + 5.0
            while time.perf_counter() < t_end:
                res_state = dut.safe_run(f'cat /sys/class/net/{cfg.interface}/operstate', timeout_s=2.0)
                if res_state.ok and 'up' in res_state.stdout.lower():
                    carrier_up = True
                    break
                time.sleep(0.5)
            if not carrier_up:
                dmesg_res = dut.safe_run(f"dmesg | grep -iE '{cfg.interface}|phy|mac' | tail -n 5")
                context_data['kernel_phy_trace'] = dmesg_res.stdout.strip() if dmesg_res.ok else ''
                logger.critical('=' * 60)
                logger.critical('fatal_phy_failed_to_achieve_carrier_lock', interface=cfg.interface)
                logger.critical('[ETH] Is the Ethernet cable unplugged? Is the PHY oscillator dead?')
                if context_data['kernel_phy_trace']:
                    logger.critical('kernel_trace_val', val=context_data['kernel_phy_trace'])
                logger.critical('=' * 60)
                return ValidatorResult(passed=False, error_msg='Physical link did not come up.', context=context_data)
            logger.debug('carrier_locked_interrogating_auto_negotiated_link_speed', interface=cfg.interface)
            res_speed = dut.safe_run(f'cat /sys/class/net/{cfg.interface}/speed', timeout_s=2.0)
            try:
                actual_speed = int(res_speed.stdout.strip())
            except ValueError:
                actual_speed = -1
            context_data['negotiated_mtu'] = cfg.mtu
            if actual_speed < cfg.expected_speed_mbps:
                logger.critical('=' * 60)
                logger.critical('fatal_degraded_silicon_or_bent_rj45_pins_detected', interface=cfg.interface)
                logger.critical('negotiated_actual_speed_mbps_expected_expected_speed_mbps_mbps', interface=cfg.interface, actual_speed=actual_speed, expected_speed_mbps=cfg.expected_speed_mbps)
                logger.critical('=' * 60)
                return ValidatorResult(passed=False, error_msg=f'Degraded PHY speed: {actual_speed} Mbps', metrics={'eth_speed_mbps': float(actual_speed)}, context=context_data)
            logger.info('physical_link_locked_securely_at_actual_speed_mbps', interface=cfg.interface, actual_speed=actual_speed)
            return ValidatorResult(passed=True, metrics={'eth_speed_mbps': float(actual_speed)}, context=context_data)
        except TransportTimeoutError:
            logger.critical('fatal_dut_completely_unresponsive_during_link_configuration_kernel_locked', interface=cfg.interface)
            return ValidatorResult(passed=False, error_msg='DUT completely unresponsive during link configuration.')
        except TransportConnectionError as e:
            logger.critical('=' * 60)
            logger.critical('fatal_transport_pipe_shattered_during_network_reset', interface=cfg.interface)
            logger.critical('[ETH] SUICIDE TRAP: Did you route an Ethernet test over the SSH transport?')
            logger.critical('=' * 60)
            return ValidatorResult(passed=False, error_msg=f'Transport pipe shattered during network reset (Routing error?): {e}')

    @classmethod
    def measure_throughput(cls, dut: DutTransport, cfg: EthernetConfig) -> ValidatorResult:
        """Measures DMA/MAC throughput by orchestrating an iperf3 client/server session.

        Spawns a local Host PC iperf3 daemon, triggers the DUT client, and parses
        the JSON results to ensure hardware physics (throughput, retransmits) meet
        the required minimums.

        Args:
            dut: The transport interface connected to the Device Under Test.
            cfg: The Ethernet configuration parameters.

        Returns:
            ValidatorResult: An object containing the validation outcome (passed/failed), 
                captured metrics like throughput and retransmits, and contextual error information.
        """
        logger.info('measuring_dma_mac_throughput_to_host_iperf_ip_for_iperf_duration_s_s', host_iperf_ip=cfg.host_iperf_ip, iperf_duration_s=cfg.iperf_duration_s)
        context_data: Dict[str, Any] = {}
        server_proc = None
        try:
            logger.debug('spawning_local_host_pc_iperf3_daemon_on_port_iperf_port', iperf_port=cfg.iperf_port)
            server_cmd = ['iperf3', '-s', '-p', str(cfg.iperf_port), '-1', '-J']
            server_proc = subprocess.Popen(server_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.5)
            if server_proc.poll() is not None:
                _, stderr = server_proc.communicate()
                logger.critical('fatal_host_pc_iperf3_server_failed_to_bind_val', val=stderr.strip())
                return ValidatorResult(passed=False, error_msg=f'Host iperf3 server failed to bind: {stderr.strip()}')
            baseline_health = collect_soc_health(dut)
            if baseline_health:
                context_data['pre_test_health'] = baseline_health
            cmd = f'iperf3 -c {cfg.host_iperf_ip} -p {cfg.iperf_port} -t {cfg.iperf_duration_s} -J'
            logger.debug('commanding_dut_to_initiate_traffic_cmd', cmd=cmd)
            result = dut.safe_run(cmd, timeout_s=cfg.iperf_duration_s + 5.0)
            post_health = collect_soc_health(dut)
            if not result.ok:
                context_data['iperf_stderr'] = result.stderr.strip()
                logger.error('target_iperf3_client_execution_failed_val', val=context_data['iperf_stderr'])
                return ValidatorResult(passed=False, error_msg='iperf3 client command failed.', context=context_data)
            try:
                data = json.loads(result.stdout)
                mbps = round(data['end']['sum_sent']['bits_per_second'] / 1000000, 2)
                context_data['cpu_utilization_percent'] = round(data['end']['cpu_utilization_percent']['host_total'], 2)
                context_data['retransmits'] = data['end']['sum_sent'].get('retransmits', 0)
            except (json.JSONDecodeError, KeyError) as e:
                logger.critical('fatal_failed_to_parse_iperf3_json_matrix_e', e=e)
                context_data['raw_output'] = result.stdout.strip()[-500:]
                return ValidatorResult(passed=False, error_msg='Malformed iperf3 output.', context=context_data)
            passed = mbps >= cfg.iperf_min_mbps
            if not passed:
                logger.critical('=' * 60)
                logger.critical('fatal_throughput_failed_dma_mac_is_degraded')
                logger.critical('clocked_mbps_mbps_required_limit_iperf_min_mbps_mbps', mbps=mbps, iperf_min_mbps=cfg.iperf_min_mbps)
                logger.critical('=' * 60)
            metrics = {'throughput_mbps': mbps, 'eth_retransmits': context_data['retransmits']}
            metrics.update(post_health)
            return ValidatorResult(passed=passed, metrics=metrics, error_msg='' if passed else f'Low throughput: {mbps} Mbps', context=context_data)
        except TransportTimeoutError:
            logger.critical('[ETH] FATAL: iperf3 execution caused a hard CPU lockup on the DUT. Check power rails.')
            return ValidatorResult(passed=False, error_msg='iperf3 execution caused a hard CPU lockup on the DUT.')
        except TransportConnectionError as e:
            logger.critical('fatal_transport_dropped_during_iperf_ssh_suicide_trap_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Transport dropped during iperf. (Did you route a destructive net test over SSH instead of Serial?): {e}')
        except Exception as e:
            logger.critical('fatal_catastrophic_host_pc_failure_during_throughput_test_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Host Execution interrupted: {e}')
        finally:
            if server_proc and server_proc.poll() is None:
                logger.debug('[ETH] ZERO-LEAKAGE: Reaping Host PC iperf3 server daemon...')
                server_proc.kill()
                server_proc.communicate()

