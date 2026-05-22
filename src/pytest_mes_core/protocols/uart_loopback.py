import structlog
import logging
import time
import anyio
from typing import List, Dict
from pytest_mes_core.transports import DutTransport
from pytest_mes_core.host_adapters import HostPeripheralSerialAdapter
logger = structlog.get_logger('mes_core.protocols.uart')

class UartTopologyValidator:
    """Validates UART topologies (RS232/RS485) across multiple physical interfaces."""

    def __init__(self, dut: DutTransport, host_adapters: Dict[str, HostPeripheralSerialAdapter]=None):
        self.dut = dut
        self.host_adapters = host_adapters or {}

    def configure_dut_interface(self, interface: str, baudrate: int=115200) -> None:
        """Configures the target UART interface natively using stty.

        Args:
            interface: The tty device path (e.g. /dev/ttyS1).
            baudrate: The serial baudrate to apply.

        Raises:
            RuntimeError: If stty fails to configure the interface.
        """
        logger.info('configuring_target_interface_interface_baudrate_bps', interface=interface, baudrate=baudrate)
        stty_cmd = f'stty -F {interface} {baudrate} raw -echo -echoe -echok -icrnl -onlcr clocal cread -crtscts'
        res = self.dut.safe_run(stty_cmd)
        if not res.ok:
            raise RuntimeError(f'Failed to configure {interface}: {res.stderr or res.stdout}')

    def _fire_and_reap(self, tx_node: str, rx_nodes: List[str], payload: bytes, timeout_s: float) -> bool:
        hex_str = payload.hex().upper()
        logger.debug('preparing_payload_hex_str', hex_str=hex_str)
        for rx in rx_nodes:
            owner, iface = rx.split(':')
            logger.debug('arming_rx_listener_on_rx', rx=rx)
            if owner == 'host':
                self.host_adapters[iface].clear_rx_buffer()
            elif owner == 'dut':
                clean_iface = iface.replace('/', '_')
                log = f'/tmp/uart_rx{clean_iface}.log'
                self.dut.safe_run('killall -9 cat')
                self.dut.safe_run(f'rm -f {log}')
                self.dut.safe_run(f"setsid sh -c 'cat {iface} > {log}' >/dev/null 2>&1 &")
        time.sleep(0.3)
        logger.debug('firing_tx_from_tx_node', tx_node=tx_node)
        tx_owner, tx_iface = tx_node.split(':')
        if tx_owner == 'host':
            self.host_adapters[tx_iface].send(payload)
        elif tx_owner == 'dut':
            bash_hex = ''.join([f'\\x{b:02X}' for b in payload])
            self.dut.safe_run(f"printf '{bash_hex}' > {tx_iface}")
        all_passed = True
        for rx in rx_nodes:
            owner, iface = rx.split(':')
            logger.debug('reaping_results_from_rx', rx=rx)
            if owner == 'host':
                if not self.host_adapters[iface].expect(payload, timeout_s):
                    logger.error('rx_failed_to_capture_payload_hex_str_from_tx_node', rx=rx, hex_str=hex_str, tx_node=tx_node)
                    all_passed = False
                else:
                    logger.info('rx_successfully_captured_hex_str', rx=rx, hex_str=hex_str)
            elif owner == 'dut':
                time.sleep(0.5)
                self.dut.safe_run('killall -9 cat')
                clean_iface = iface.replace('/', '_')
                res = self.dut.safe_run(f"""hexdump -v -e '/1 "%02X"' /tmp/uart_rx{clean_iface}.log""")
                if hex_str not in res.stdout.upper():
                    logger.error('rx_failed_to_capture_hex_str_from_tx_node_hex_buffer_stdout', rx=rx, hex_str=hex_str, tx_node=tx_node, stdout=res.stdout)
                    all_passed = False
                else:
                    logger.info('rx_successfully_captured_hex_str', rx=rx, hex_str=hex_str)
        return all_passed

    def validate_topology(self, nodes: List[str], base_payload: bytes, timeout_s: float=2.0) -> bool:
        """Validates a multi-node serial topology using round-robin transmissions.

        Args:
            nodes: A list of nodes in 'owner:interface' format.
            base_payload: The base bytes payload to transmit.
            timeout_s: Time to wait for the payload to loop back.

        Returns:
            bool: True if all nodes transmitted and received the payload successfully.
        """
        logger.info('validating_full_matrix_for_nodes_nodes', nodes=nodes)
        all_passed = True
        for index, tx_node in enumerate(nodes):
            rx_nodes = [n for n in nodes if n != tx_node]
            current_payload = base_payload + bytes([index])
            logger.info('round_robin_pass_val_tx_tx_node_rx_rx_nodes', val=index + 1, tx_node=tx_node, rx_nodes=rx_nodes)
            if not self._fire_and_reap(tx_node, rx_nodes, current_payload, timeout_s):
                all_passed = False
            time.sleep(0.2)
        return all_passed

    async def async_configure_dut_interface(self, interface: str, baudrate: int=115200) -> None:
        logger.info('configuring_target_interface_interface_baudrate_bps', interface=interface, baudrate=baudrate)
        stty_cmd = f'stty -F {interface} {baudrate} raw -echo -echoe -echok -icrnl -onlcr clocal cread -crtscts'
        res = await self.dut.async_safe_run(stty_cmd)
        if not res.ok:
            raise RuntimeError(f'Failed to configure {interface}: {res.stderr or res.stdout}')

    async def async__fire_and_reap(self, tx_node: str, rx_nodes: List[str], payload: bytes, timeout_s: float) -> bool:
        hex_str = payload.hex().upper()
        logger.debug('preparing_payload_hex_str', hex_str=hex_str)
        for rx in rx_nodes:
            owner, iface = rx.split(':')
            logger.debug('arming_rx_listener_on_rx', rx=rx)
            if owner == 'host':
                self.host_adapters[iface].clear_rx_buffer()
            elif owner == 'dut':
                clean_iface = iface.replace('/', '_')
                log = f'/tmp/uart_rx{clean_iface}.log'
                await self.dut.async_safe_run('killall -9 cat')
                await self.dut.async_safe_run(f'rm -f {log}')
                await self.dut.async_safe_run(f"setsid sh -c 'cat {iface} > {log}' >/dev/null 2>&1 &")
        await anyio.sleep(0.3)
        logger.debug('firing_tx_from_tx_node', tx_node=tx_node)
        tx_owner, tx_iface = tx_node.split(':')
        if tx_owner == 'host':
            await self.host_adapters[tx_iface].async_send(payload)
        elif tx_owner == 'dut':
            bash_hex = ''.join([f'\\x{b:02X}' for b in payload])
            await self.dut.async_safe_run(f"printf '{bash_hex}' > {tx_iface}")
        all_passed = True
        for rx in rx_nodes:
            owner, iface = rx.split(':')
            logger.debug('reaping_results_from_rx', rx=rx)
            if owner == 'host':
                if not await self.host_adapters[iface].async_expect(payload, timeout_s):
                    logger.error('rx_failed_to_capture_payload_hex_str_from_tx_node', rx=rx, hex_str=hex_str, tx_node=tx_node)
                    all_passed = False
                else:
                    logger.info('rx_successfully_captured_hex_str', rx=rx, hex_str=hex_str)
            elif owner == 'dut':
                await anyio.sleep(0.5)
                await self.dut.async_safe_run('killall -9 cat')
                clean_iface = iface.replace('/', '_')
                res = await self.dut.async_safe_run(f"""hexdump -v -e '/1 "%02X"' /tmp/uart_rx{clean_iface}.log""")
                if hex_str not in res.stdout.upper():
                    logger.error('rx_failed_to_capture_hex_str_from_tx_node_hex_buffer_stdout', rx=rx, hex_str=hex_str, tx_node=tx_node, stdout=res.stdout)
                    all_passed = False
                else:
                    logger.info('rx_successfully_captured_hex_str', rx=rx, hex_str=hex_str)
        return all_passed

    async def async_validate_topology(self, nodes: List[str], base_payload: bytes, timeout_s: float=2.0) -> bool:
        logger.info('validating_full_matrix_for_nodes_nodes', nodes=nodes)
        all_passed = True
        for index, tx_node in enumerate(nodes):
            rx_nodes = [n for n in nodes if n != tx_node]
            current_payload = base_payload + bytes([index])
            logger.info('round_robin_pass_val_tx_tx_node_rx_rx_nodes', val=index + 1, tx_node=tx_node, rx_nodes=rx_nodes)
            if not await self.async__fire_and_reap(tx_node, rx_nodes, current_payload, timeout_s):
                all_passed = False
            await anyio.sleep(0.2)
        return all_passed