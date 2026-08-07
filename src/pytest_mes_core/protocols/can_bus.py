import functools
import structlog
import logging
import time
import anyio
from typing import List, Dict
from pytest_mes_core.transports import DutTransport
from pytest_mes_core.host_adapters import HostCanAdapter
logger = structlog.get_logger('mes_core.protocols.can')

class CanTopologyValidator:
    """Validates CAN bus topology by orchestrating round-robin TX/RX tests.

    Manages both Target DUT CAN interfaces via POSIX iproute2/can-utils
    and local Host PC CAN adapters (e.g. PEAK PCAN) to ensure electrical
    and software bus integrity.
    """

    def __init__(self, dut: DutTransport, host_adapters: Dict[str, HostCanAdapter]=None):
        self.dut = dut
        self.host_adapters = host_adapters or {}

    def configure_dut_interface(self, interface: str, bitrate: int=500000) -> None:
        """Configures the Target DUT CAN interface.

        Clears existing error states, applies the bitrate, and brings the link UP.
        Validates the state mathematically by parsing `ip link show`.

        Args:
            interface: The name of the interface on the DUT (e.g. 'can0').
            bitrate: The CAN bus bitrate in bits per second.

        Raises:
            RuntimeError: If the interface fails to configure or transition to the UP state.
        """
        logger.info('configuring_target_interface_interface_bitrate_bps', interface=interface, bitrate=bitrate)
        self.dut.safe_run(f'ip link set {interface} down')
        time.sleep(0.1)
        res_cfg = self.dut.safe_run(f'ip link set {interface} type can bitrate {bitrate}')
        if not res_cfg.ok:
            raise RuntimeError(f'Failed to set bitrate {bitrate} on {interface}: {res_cfg.stderr or res_cfg.stdout}')
        res_up = self.dut.safe_run(f'ip link set {interface} up')
        if not res_up.ok:
            raise RuntimeError(f'Failed to bring {interface} UP: {res_up.stderr or res_up.stdout}')
        verify = self.dut.safe_run(f'ip link show {interface}')
        if 'UP' not in verify.stdout:
            raise RuntimeError(f"Hardware Fault: {interface} refused to transition to UP state. Output: '{verify.stdout}'")

    def teardown_dut_interface(self, interface: str) -> None:
        """Zero-leakage teardown. Returns the Target PCB to a clean state.

        Args:
            interface: The name of the interface on the DUT to tear down.
        """
        logger.debug('tearing_down_target_interface', interface=interface)
        self.dut.safe_run(f'ip link set {interface} down')

    def _fire_and_reap(self, tx_node: str, rx_nodes: List[str], can_id: int, payload: bytes, timeout_s: float) -> bool:
        hex_data = ''.join([f'{b:02X}' for b in payload])
        linux_frame = f'{can_id:03X}#{hex_data}'
        logger.debug('preparing_frame_linux_frame', linux_frame=linux_frame)
        for rx in rx_nodes:
            owner, iface = rx.split(':')
            logger.debug('arming_rx_listener_on_rx', rx=rx)
            if owner == 'host':
                self.host_adapters[iface].clear_rx_buffer()
            elif owner == 'dut':
                log = f'/tmp/can_rx_{iface}.log'
                self.dut.safe_run('killall -9 candump')
                self.dut.safe_run(f'rm -f {log}')
                self.dut.safe_run(f"setsid sh -c 'candump {iface} -n 1 > {log}' >/dev/null 2>&1 &")
        time.sleep(0.3)
        logger.debug('firing_tx_from_tx_node', tx_node=tx_node)
        tx_owner, tx_iface = tx_node.split(':')
        if tx_owner == 'host':
            self.host_adapters[tx_iface].send(can_id, payload)
        elif tx_owner == 'dut':
            res = self.dut.safe_run(f'cansend {tx_iface} {linux_frame}')
            if not res.ok:
                logger.error('cansend_failed_on_tx_iface_stderr_stderr', tx_iface=tx_iface, stderr=res.stderr)
        all_passed = True
        for rx in rx_nodes:
            owner, iface = rx.split(':')
            logger.debug('reaping_results_from_rx', rx=rx)
            if owner == 'host':
                if not self.host_adapters[iface].expect(can_id, payload, timeout_s):
                    logger.error('rx_failed_to_capture_linux_frame_from_tx_node', rx=rx, linux_frame=linux_frame, tx_node=tx_node)
                    all_passed = False
                else:
                    logger.info('rx_successfully_captured_linux_frame', rx=rx, linux_frame=linux_frame)
            elif owner == 'dut':
                time.sleep(0.5)
                self.dut.safe_run('killall -9 candump')
                res = self.dut.safe_run(f'cat /tmp/can_rx_{iface}.log')
                stdout_clean = res.stdout.strip()
                if hex_data.upper() not in stdout_clean.replace(' ', '').upper() or f'{can_id:03X}' not in stdout_clean.upper():
                    logger.error('rx_failed_to_capture_linux_frame_from_tx_node_buffer_stdout_clean', rx=rx, linux_frame=linux_frame, tx_node=tx_node, stdout_clean=stdout_clean)
                    all_passed = False
                else:
                    logger.info('rx_successfully_captured_linux_frame', rx=rx, linux_frame=linux_frame)
        return all_passed

    def validate_topology(self, nodes: List[str], can_id_base: int, payload: bytes, timeout_s: float=2.0) -> bool:
        """Validates the full CAN matrix by forcing each node to TX while all others RX.

        Args:
            nodes: A list of nodes to test in the format 'owner:interface' (e.g., 'host:can0', 'dut:can1').
            can_id_base: The base CAN ID to use for the payload frames.
            payload: The payload bytes to transmit.
            timeout_s: The maximum time to wait for the RX nodes to capture the frame.

        Returns:
            bool: True if all nodes successfully transmitted and received the payload, False otherwise.
        """
        logger.info('validating_full_matrix_for_nodes_nodes', nodes=nodes)
        all_passed = True
        for index, tx_node in enumerate(nodes):
            rx_nodes = [n for n in nodes if n != tx_node]
            current_id = can_id_base + index
            logger.info('round_robin_pass_val_tx_tx_node_rx_rx_nodes', val=index + 1, tx_node=tx_node, rx_nodes=rx_nodes)
            if not self._fire_and_reap(tx_node, rx_nodes, current_id, payload, timeout_s):
                all_passed = False
            time.sleep(0.2)
        return all_passed


