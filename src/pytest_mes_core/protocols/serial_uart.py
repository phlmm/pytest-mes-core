import time
import logging
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.host_adapters import HostSerialAdapter

logger = logging.getLogger("mes_core.protocols.uart")

class UartEchoValidator:
    """
    Validates physical UART/RS-232/RS-485 interfaces.
    Features surgical background process management, EMI-resistant host buffering,
    and hardware-level framing error detection.
    """

    @staticmethod
    def verify_echo(
        host_ser: HostSerialAdapter,
        dut: DutTransport,
        dut_device: str,
        test_string: str = "MES_UART_SYNC"
    ) -> ValidatorResult:

        context_data: Dict[str, Any] = {"dut_device": dut_device, "baudrate": host_ser.baudrate}
        pid_file = f"/tmp/mes_uart_echo_{dut_device.replace('/', '_')}.pid"

        try:
            # ==========================================
            # 1. CLEAN SLATE & STTY CONFIGURATION
            # ==========================================
            # raw: Disables kernel line-editing (canonical mode) so bytes pass through untouched
            # -echo: Prevents the kernel from automatically echoing, avoiding double-echo infinite loops
            stty_cmd = f"stty -F {dut_device} {host_ser.baudrate} raw -echo"
            res_stty = dut.safe_run(stty_cmd, timeout_s=3.0)

            if not res_stty.ok:
                logger.error(f"[UART] Failed to configure {dut_device}. Port doesn't exist? {res_stty.stderr.strip()}")
                return ValidatorResult(passed=False, error_msg=f"stty configuration failed: {res_stty.stderr.strip()}", context=context_data)

            # ==========================================
            # 2. SURGICAL BACKGROUND LOOPBACK
            # ==========================================
            # We spawn the echo process and capture its EXACT process ID into a temporary file.
            # >/dev/null 2>&1 prevents open POSIX pipes from hanging the transport.
            loopback_cmd = f"cat {dut_device} > {dut_device} 2>/dev/null & echo $! > {pid_file}"
            dut.safe_run(loopback_cmd, timeout_s=3.0)
            time.sleep(0.2) # Allow OS to context-switch and bind the file descriptor

            # ==========================================
            # 3. HOST PC: FLUSH & TRANSMIT
            # ==========================================
            if not host_ser.ser or not host_ser.ser.is_open:
                return ValidatorResult(passed=False, error_msg="Host PC Serial Adapter is closed or disconnected.", context=context_data)

            try:
                # Defensive: Hardware lines floating during jig insertion inject EMI garbage. Flush it.
                host_ser.ser.reset_input_buffer()
                host_ser.ser.reset_output_buffer()

                payload = (test_string + "\n").encode('utf-8')
                t0 = time.perf_counter()

                host_ser.ser.write(payload)
                host_ser.ser.flush() # Block until OS physically shifts out the last bit on the FTDI chip

                # ==========================================
                # 4. HOST PC: RECEIVE & DECODE
                # ==========================================
                # errors='replace' prevents a single electrical bit-flip from crashing pytest with UnicodeDecodeError
                raw_response = host_ser.ser.readline()
                latency = round((time.perf_counter() - t0) * 1000.0, 2)

                response = raw_response.decode('utf-8', errors='replace').strip()
                context_data["raw_rx_bytes"] = raw_response.hex()
                context_data["decoded_rx"] = response

            except Exception as host_err:
                # Catch pyserial exceptions (e.g., operator unplugs the USB to UART cable mid-test)
                logger.critical(f"[UART] Host adapter shattered during transmission: {host_err}")
                return ValidatorResult(passed=False, error_msg=f"Host PC Serial Fault: {host_err}", context=context_data)

            # ==========================================
            # 5. LOGICAL EVALUATION
            # ==========================================
            if not response:
                logger.error("[UART] Timeout. No data echoed back from DUT.")
                return ValidatorResult(passed=False, error_msg="Timeout: No echo received (Check TX/RX wiring).", context=context_data)

            if test_string in response:
                logger.info(f"[UART] Loopback sync verified in {latency}ms.")
                return ValidatorResult(
                    passed=True,
                    metrics={"uart_latency_ms": latency},
                    context=context_data
                )

            logger.warning(f"[UART] Garbage data received: '{response}'. Framing error or EMI noise?")
            return ValidatorResult(
                passed=False,
                error_msg=f"Garbage response received: '{response}' (Check baudrate/EMI)",
                context=context_data
            )

        except TransportTimeoutError:
            return ValidatorResult(passed=False, error_msg="DUT hung while configuring UART loopback.", context=context_data)
        except TransportConnectionError as e:
            return ValidatorResult(passed=False, error_msg=f"Transport pipe shattered during UART setup: {e}", context=context_data)

        finally:
            # ==========================================
            # 6. SURGICAL ZERO-LEAKAGE TEARDOWN
            # ==========================================
            if dut.is_connected:
                try:
                    logger.debug(f"[UART] ZERO-LEAKAGE: Releasing DUT loopback on {dut_device}.")
                    # Surgically kill ONLY the specific background process we spawned
                    dut.safe_run(f"kill -9 $(cat {pid_file} 2>/dev/null) >/dev/null 2>&1 || true", timeout_s=3.0)
                    dut.safe_run(f"rm -f {pid_file} >/dev/null 2>&1 || true", timeout_s=3.0)
                except Exception as cleanup_err:
                    logger.debug(f"[UART] Loopback cleanup failed: {cleanup_err}")
