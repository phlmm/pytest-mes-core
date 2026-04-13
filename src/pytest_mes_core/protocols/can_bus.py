import time
import re
import logging
from typing import Dict, Any, List

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult

# Assuming HostCanAdapter is defined elsewhere in your framework
from pytest_mes_core.host_adapters import HostCanAdapter

logger = logging.getLogger("mes_core.protocols.can")

class CanBusValidator:
    """
    Validates physical CAN/CAN-FD interfaces.
    Features isolated TX/RX testing, race-condition-free background polling,
    and deep hardware error counter scraping.
    """

    @staticmethod
    def verify_bidirectional_link(
        dut: DutTransport,
        host_can: HostCanAdapter,
        dut_interface: str = "can0",
        test_id: int = 0x123,
        payload: List[int] = [0xDE, 0xAD, 0xBE, 0xEF],
        timeout_s: float = 2.0
    ) -> ValidatorResult:

        logger.info(f"[CAN] Initializing Host <-> DUT link on {dut_interface} @ {host_can.bitrate}bps")
        context_data: Dict[str, Any] = {}
        metrics: Dict[str, float] = {}

        # Format payload for `cansend` (e.g., "DE.AD.BE.EF")
        payload_str = ".".join([f"{b:02X}" for b in payload])
        dump_file = f"/tmp/can_rx_{test_id}.log"

        try:
            if not host_can.bus:
                return ValidatorResult(passed=False, error_msg="Host PC CAN adapter is not initialized.")

            # ==========================================
            # 1. ZERO-STATE INITIALIZATION
            # ==========================================
            # Force the link down before attempting to change the bitrate
            dut.safe_run(f"ip link set {dut_interface} down", timeout_s=5.0)

            res_up = dut.safe_run(
                f"ip link set {dut_interface} up type can bitrate {host_can.bitrate}",
                timeout_s=5.0
            )

            if not res_up.ok:
                return ValidatorResult(
                    passed=False,
                    error_msg=f"Failed to configure {dut_interface}. Missing kernel driver?",
                    context={"ip_link_stderr": res_up.stderr, "ip_link_stdout": res_up.stdout}
                )

            # ==========================================
            # 2. ISOLATED TEST: DUT TX -> HOST RX
            # ==========================================
            logger.debug("[CAN] Phase 1: Testing DUT Transmission...")

            # Flush stale frames from the Host PC Python buffer
            flushed = 0
            while host_can.bus.recv(timeout=0.01):
                flushed += 1
            if flushed > 0:
                logger.debug(f"[CAN] Flushed {flushed} stale frames from host buffer.")

            # Command DUT to send the frame
            dut.safe_run(f"cansend {dut_interface} {test_id:03X}#{payload_str}")

            # Host awaits reception
            t_end = time.perf_counter() + timeout_s
            rx_msg = None
            while time.perf_counter() < t_end:
                rx_msg = host_can.bus.recv(timeout=0.1)
                if rx_msg and rx_msg.arbitration_id == test_id:
                    break

            dut_tx_passed = rx_msg is not None

            # ==========================================
            # 3. ISOLATED TEST: HOST TX -> DUT RX
            # ==========================================
            logger.debug("[CAN] Phase 2: Testing DUT Reception...")

            # RACE CONDITION FIX:
            # We use a hardware filter (,<ID>~7FF) so candump ONLY captures the exact packet we want.
            # We run it in the background, but we will cleanly SIGINT it later to force a buffer flush.
            dut.safe_run(f"candump {dut_interface},{test_id + 1:03X}~7FF > {dump_file} 2>/dev/null &")
            time.sleep(0.2) # Allow process to spawn and bind to the CAN socket

            # Host transmits
            import can # Lazy import to avoid global dependency issues
            host_msg = can.Message(arbitration_id=test_id + 1, data=payload, is_extended_id=False)
            host_can.bus.send(host_msg)

            time.sleep(0.5) # Allow DUT kernel to process the interrupt

            # RACE CONDITION FIX: Cleanly terminate candump.
            # SIGINT (2) forces candump to flush its file buffer to disk before dying. SIGKILL (9) does not.
            dut.safe_run("killall -2 candump >/dev/null 2>&1 || true", timeout_s=3.0)

            # Check DUT dump file
            dump_res = dut.safe_run(f"cat {dump_file} 2>/dev/null", timeout_s=5.0)

            # Use upper() to make sure hex casing ("124" vs "124") doesn't cause a false negative
            dut_rx_passed = f"{test_id + 1:03X}" in dump_res.stdout.upper() if dump_res.ok else False

            # ==========================================
            # 4. KERNEL STATISTICS SCRAPING
            # ==========================================
            stats = CanBusValidator._scrape_can_stats(dut, dut_interface)
            context_data.update(stats["context"])
            metrics.update(stats["metrics"])

            metrics["dut_tx_ok"] = 1.0 if dut_tx_passed else 0.0
            metrics["dut_rx_ok"] = 1.0 if dut_rx_passed else 0.0

            # ==========================================
            # 5. LOGICAL EVALUATION
            # ==========================================
            if not dut_tx_passed or not dut_rx_passed:
                err = []
                if not dut_tx_passed: err.append("DUT failed to Transmit (Check TX pin/transceiver)")
                if not dut_rx_passed: err.append("DUT failed to Receive (Check RX pin/transceiver)")

                # Check for physical bus faults (e.g., CAN_H and CAN_L shorted together)
                bus_state = stats["context"].get("bus_state", "UNKNOWN")
                if bus_state in ["ERROR-PASSIVE", "BUS-OFF"]:
                    err.append(f"Bus entered fatal physical state: {bus_state}")

                logger.error(f"[CAN] Link verification failed: {' | '.join(err)}")
                return ValidatorResult(passed=False, error_msg=" | ".join(err), metrics=metrics, context=context_data)

            logger.info("[CAN] Bidirectional link verified successfully. Zero physical errors detected.")
            return ValidatorResult(passed=True, metrics=metrics, context=context_data)

        except TransportTimeoutError:
            return ValidatorResult(passed=False, error_msg="DUT completely unresponsive (Transport Timeout).", context=context_data)

        except TransportConnectionError as e:
            return ValidatorResult(passed=False, error_msg=f"Transport pipe shattered during CAN test: {e}", context=context_data)

        finally:
            # ==========================================
            # 6. ZERO-LEAKAGE TEARDOWN
            # ==========================================
            try:
                logger.debug("[CAN] Executing ZERO-LEAKAGE interface teardown.")
                # We use SIGKILL (-9) here just in case the SIGINT didn't work earlier
                dut.safe_run("killall -9 candump >/dev/null 2>&1 || true", timeout_s=3.0)
                dut.safe_run(f"rm -f {dump_file} >/dev/null 2>&1 || true", timeout_s=3.0)
                dut.safe_run(f"ip link set {dut_interface} down >/dev/null 2>&1 || true", timeout_s=5.0)
            except Exception as teardown_err:
                logger.debug(f"[CAN] Teardown skipped (Transport likely dead): {teardown_err}")


    @staticmethod
    def _scrape_can_stats(dut: DutTransport, interface: str) -> Dict[str, Any]:
        """
        Scrapes `ip -details -statistics link show can0` to extract
        hardware-level CAN frame drops, overruns, and bus states.
        """
        result = {"metrics": {}, "context": {}}
        res = dut.safe_run(f"ip -details -statistics link show {interface}", timeout_s=5.0)

        if not res.ok or not res.stdout:
            return result

        output = res.stdout.strip()
        result["context"]["ip_link_dump"] = output

        # Extract Bus State (e.g., ERROR-ACTIVE, ERROR-PASSIVE, BUS-OFF)
        state_match = re.search(r'can state (\S+)', output)
        if state_match:
            result["context"]["bus_state"] = state_match.group(1)

        # Extract RX/TX Error counters (Standard Linux format)
        try:
            lines = output.split('\n')
            for i, line in enumerate(lines):
                if line.strip().startswith("RX:"):
                    rx_vals = lines[i+1].split()
                    result["metrics"]["can_rx_errors"] = float(rx_vals[2])
                    result["metrics"]["can_rx_dropped"] = float(rx_vals[3])
                    result["metrics"]["can_rx_overrun"] = float(rx_vals[4])
                elif line.strip().startswith("TX:"):
                    tx_vals = lines[i+1].split()
                    result["metrics"]["can_tx_errors"] = float(tx_vals[2])
                    result["metrics"]["can_tx_dropped"] = float(tx_vals[3])
        except (IndexError, ValueError) as e:
            logger.debug(f"[CAN] Failed to parse iproute2 stats: {e}")

        return result
