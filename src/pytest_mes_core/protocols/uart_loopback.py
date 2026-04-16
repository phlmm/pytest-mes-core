import logging
import time
from typing import List, Dict
from pytest_mes_core.transports import DutTransport
from pytest_mes_core.host_adapters import HostPeripheralSerialAdapter

logger = logging.getLogger("mes_core.protocols.uart")

class UartTopologyValidator:
    def __init__(self, dut: DutTransport, host_adapters: Dict[str, HostPeripheralSerialAdapter] = None):
        self.dut = dut
        self.host_adapters = host_adapters or {}

    def configure_dut_interface(self, interface: str, baudrate: int = 115200) -> None:
        logger.info(f"[DUT UART] Configuring Target Interface '{interface}' @ {baudrate}bps...")

        # 1. TTY Configuration: Disable echo, ignore modem pins (clocal), enable rx (cread), disable flow control
        stty_cmd = f"stty -F {interface} {baudrate} raw -echo -echoe -echok -icrnl -onlcr clocal cread -crtscts"
        res = self.dut.safe_run(stty_cmd)

        if not res.ok:
            raise RuntimeError(f"Failed to configure {interface}: {res.stderr or res.stdout}")

    def _fire_and_reap(self, tx_node: str, rx_nodes: List[str], payload: bytes, timeout_s: float) -> bool:
        hex_str = payload.hex().upper()
        logger.debug(f"[RS485 TRACE] --- Preparing Payload [{hex_str}] ---")

        # Arm RX Nodes
        for rx in rx_nodes:
            owner, iface = rx.split(":")
            logger.debug(f"[RS485 TRACE] Arming RX Listener on {rx}...")

            if owner == "host":
                self.host_adapters[iface].clear_rx_buffer()
            elif owner == "dut":
                clean_iface = iface.replace("/", "_")
                log = f"/tmp/uart_rx{clean_iface}.log"
                self.dut.safe_run("killall -9 cat")
                self.dut.safe_run(f"rm -f {log}")
                self.dut.safe_run(f"setsid sh -c 'cat {iface} > {log}' >/dev/null 2>&1 &")

        time.sleep(0.3)

        # Fire TX
        logger.debug(f"[RS485 TRACE] Firing TX from {tx_node}...")
        tx_owner, tx_iface = tx_node.split(":")

        if tx_owner == "host":
            self.host_adapters[tx_iface].send(payload)
        elif tx_owner == "dut":
            bash_hex = "".join([f"\\x{b:02X}" for b in payload])
            self.dut.safe_run(f"printf '{bash_hex}' > {tx_iface}")

        # Reap RX
        all_passed = True
        for rx in rx_nodes:
            owner, iface = rx.split(":")
            logger.debug(f"[RS485 TRACE] Reaping results from {rx}...")

            if owner == "host":
                if not self.host_adapters[iface].expect(payload, timeout_s):
                    logger.error(f"[RS485 FAIL] {rx} failed to capture payload [{hex_str}] from {tx_node}.")
                    all_passed = False
                else:
                    logger.info(f"[RS485 OK] {rx} successfully captured [{hex_str}]")

            elif owner == "dut":
                time.sleep(0.5)
                # Kill the background cat stream safely
                self.dut.safe_run("killall -9 cat")

                clean_iface = iface.replace("/", "_")
                res = self.dut.safe_run(f"hexdump -v -e '/1 \"%02X\"' /tmp/uart_rx{clean_iface}.log")

                if hex_str not in res.stdout.upper():
                    logger.error(f"[RS485 FAIL] {rx} failed to capture [{hex_str}] from {tx_node}. Hex Buffer: '{res.stdout}'")
                    all_passed = False
                else:
                    logger.info(f"[RS485 OK] {rx} successfully captured [{hex_str}]")

        return all_passed

    def validate_topology(self, nodes: List[str], base_payload: bytes, timeout_s: float = 2.0) -> bool:
        logger.info(f"[RS485 Topology] Validating Full Matrix for Nodes: {nodes}")
        all_passed = True

        for index, tx_node in enumerate(nodes):
            rx_nodes = [n for n in nodes if n != tx_node]
            # Mutate the payload slightly for each pass to ensure we aren't reading stale buffers
            current_payload = base_payload + bytes([index])

            logger.info(f"[RS485 Topology] -> Round-Robin Pass {index+1}: TX: {tx_node} -> RX: {rx_nodes}")

            if not self._fire_and_reap(tx_node, rx_nodes, current_payload, timeout_s):
                all_passed = False
            time.sleep(0.2)

        return all_passed
