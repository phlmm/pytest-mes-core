import time
import logging
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.host_adapters import HostCanAdapter
from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.protocols.can")

class CanLoopbackValidator:
    @staticmethod
    def verify_bidirectional_echo(
        dut_ssh: EphemeralSSHClient,
        host_can: HostCanAdapter,
        dut_interface: str,
        test_id: int = 0x123,
        payload: list[int] = [0xDE, 0xAD, 0xBE, 0xEF],
        timeout_s: float = 2.0
    ) -> ValidatorResult:

        logger.info(f"[CAN] Initializing Host <-> DUT loopback on {dut_interface} @ {host_can.bitrate}bps")

        # 1. Violent Teardown of prior states before beginning
        dut_ssh.conn.run(f"ip link set {dut_interface} down || true", hide=True, warn=True)
        dut_ssh.conn.run("killall candump || true", hide=True, warn=True)

        # 2. Setup DUT Echo Server
        dut_ssh.conn.run(f"ip link set {dut_interface} up type can bitrate {host_can.bitrate}", hide=True)
        echo_cmd = f"candump {dut_interface} | awk '{{print \"{hex(test_id + 1)}#\"$3}}' | xargs -I{{}} cansend {dut_interface} {{}} &"
        dut_ssh.conn.run(echo_cmd, hide=True)
        time.sleep(0.5) # OS scheduler allowance

        try:
            if not host_can.bus: raise RuntimeError("FATAL: Host CAN socket closed.")
            msg = __import__('can').Message(arbitration_id=test_id, data=payload, is_extended_id=False)

            # Defensive: Flush stale frames from prior tests that might falsely trigger success
            flushed = 0
            while host_can.bus.recv(timeout=0.01): flushed += 1
            if flushed > 0: logger.debug(f"[CAN] Flushed {flushed} stale frames from host buffer.")

            t0 = time.perf_counter()
            host_can.bus.send(msg)

            # Await Echo
            t_end = time.perf_counter() + timeout_s
            while time.perf_counter() < t_end:
                rx_msg = host_can.bus.recv(timeout=0.1)
                if rx_msg and rx_msg.arbitration_id == test_id + 1:
                    latency = round((time.perf_counter() - t0) * 1000.0, 2)
                    logger.info(f"[CAN] Hardware loopback successful. Latency: {latency}ms")
                    return ValidatorResult(
                        passed=True,
                        metrics={"can_latency_ms": latency},
                        context={"rx_id": hex(rx_msg.arbitration_id)}
                    )

            logger.error("[CAN] TIMEOUT. No echo received from DUT. ISO1042 isolator blown?")
            return ValidatorResult(passed=False, error_msg="Timeout waiting for DUT CAN echo.")

        finally:
            logger.debug("[CAN] Executing ZERO-LEAKAGE interface teardown.")
            dut_ssh.conn.run("killall candump || true", hide=True, warn=True)
            dut_ssh.conn.run(f"ip link set {dut_interface} down || true", hide=True, warn=True)
