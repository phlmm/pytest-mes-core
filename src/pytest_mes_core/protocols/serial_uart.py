import time
import logging
from tenacity import retry, stop_after_attempt, wait_fixed
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.host_adapters import HostSerialAdapter
from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.protocols.uart")

class UartEchoValidator:
    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(0.5), reraise=True)
    def verify_echo(
        host_ser: HostSerialAdapter,
        dut_ssh: EphemeralSSHClient,
        dut_device: str,
        test_string: str = "MES_UART_SYNC"
    ) -> ValidatorResult:

        # 1. Clean Slate DUT
        dut_ssh.conn.run("killall cat || true", hide=True, warn=True)
        dut_ssh.conn.run(f"stty -F {dut_device} {host_ser.baudrate} raw -echo", hide=True)

        # 2. Spawn simple background echo process on the DUT
        dut_ssh.conn.run(f"cat {dut_device} > {dut_device} &", hide=True)
        time.sleep(0.1)

        try:
            if not host_ser.ser: raise RuntimeError("Host Serial closed.")

            # Defensive: Flush Host buffers. Hardware lines floating during jig insertion inject garbage.
            host_ser.ser.reset_input_buffer()
            host_ser.ser.reset_output_buffer()

            # 3. Write
            payload = (test_string + "\n").encode('utf-8')
            t0 = time.perf_counter()
            host_ser.ser.write(payload)
            host_ser.ser.flush() # Block until OS physically shifts out the last bit

            # 4. Read Response (Defensive timeout and decoding)
            # errors='replace' prevents a single bit-flip from crashing pytest with UnicodeDecodeError
            response = host_ser.ser.readline().decode('utf-8', errors='replace').strip()
            latency = round((time.perf_counter() - t0) * 1000.0, 2)

            if test_string in response:
                logger.info(f"[UART] Loopback sync verified in {latency}ms.")
                return ValidatorResult(passed=True, metrics={"uart_latency_ms": latency})

            logger.warning(f"[UART] Garbage data received: '{response}'. EMI noise?")
            return ValidatorResult(
                passed=False,
                error_msg=f"Garbage or empty response received: '{response}'"
            )

        finally:
            logger.debug("[UART] ZERO-LEAKAGE: Killing DUT cat loopback.")
            dut_ssh.conn.run("killall cat || true", hide=True, warn=True)
