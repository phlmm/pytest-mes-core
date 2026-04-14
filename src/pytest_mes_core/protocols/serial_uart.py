# src/pytest_mes_core/protocols/serial_uart.py
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

        logger.info(f"[UART] Initiating hardware loopback test on {dut_device} at {host_ser.baudrate} baud...")

        try:
            # ==========================================
            # 1. CLEAN SLATE & STTY CONFIGURATION
            # ==========================================
            # raw: Disables kernel line-editing (canonical mode) so bytes pass through untouched
            # -echo: Prevents the kernel from automatically echoing, avoiding double-echo infinite loops
            logger.debug(f"[UART] Forcing DUT port {dut_device} into raw/no-echo mode...")
            stty_cmd = f"stty -F {dut_device} {host_ser.baudrate} raw -echo"
            res_stty = dut.safe_run(stty_cmd, timeout_s=3.0)

            if not res_stty.ok:
                logger.critical("="*60)
                logger.critical(f"[UART] FATAL: Failed to configure DUT port {dut_device}!")
                logger.critical(f"[UART] Does the port exist? Is the kernel UART driver loaded?")
                logger.critical(f"[UART] Kernel Stderr: {res_stty.stderr.strip()}")
                logger.critical("="*60)
                return ValidatorResult(passed=False, error_msg=f"stty configuration failed: {res_stty.stderr.strip()}", context=context_data)

            # ==========================================
            # 2. SURGICAL BACKGROUND LOOPBACK
            # ==========================================
            # We spawn the echo process and capture its EXACT process ID into a temporary file.
            # >/dev/null 2>&1 prevents open POSIX pipes from hanging the transport.
            logger.debug(f"[UART] Spawning isolated background echo daemon on DUT...")
            loopback_cmd = f"cat {dut_device} > {dut_device} 2>/dev/null & echo $! > {pid_file}"
            dut.safe_run(loopback_cmd, timeout_s=3.0)
            time.sleep(0.2) # Allow OS to context-switch and bind the file descriptor

            # ==========================================
            # 3. HOST PC: FLUSH & TRANSMIT
            # ==========================================
            if not host_ser.ser or not host_ser.ser.is_open:
                err_msg = "Host PC Serial Adapter is closed or physically disconnected."
                logger.error(f"[UART] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)

            try:
                # Defensive: Hardware lines floating during jig insertion inject EMI garbage. Flush it.
                logger.debug("[UART] Flushing physical FTDI/UART hardware buffers on Host PC...")
                host_ser.ser.reset_input_buffer()
                host_ser.ser.reset_output_buffer()

                payload = (test_string + "\n").encode('utf-8')

                logger.debug(f"[UART] TX -> '{test_string}'")
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

                logger.debug(f"[UART] RX <- '{response}' (Raw Hex: {context_data['raw_rx_bytes']})")

            except Exception as host_err:
                # Catch pyserial exceptions (e.g., operator unplugs the USB to UART cable mid-test)
                logger.critical("="*60)
                logger.critical(f"[UART] FATAL: Host PC Adapter shattered during physical transmission!")
                logger.critical(f"[UART] Was the FTDI/USB cable physically unplugged? Err: {host_err}")
                logger.critical("="*60)
                return ValidatorResult(passed=False, error_msg=f"Host PC Serial Fault: {host_err}", context=context_data)

            # ==========================================
            # 5. LOGICAL EVALUATION
            # ==========================================
            if not response:
                logger.critical("="*60)
                logger.critical(f"[UART] FATAL: Timeout! Zero bytes echoed back from DUT.")
                logger.critical(f"[UART] Check physical TX/RX wiring, crossover cables (Null Modem), and voltage levels.")
                logger.critical("="*60)
                return ValidatorResult(passed=False, error_msg="Timeout: No echo received (Check TX/RX wiring).", context=context_data)

            if test_string in response:
                logger.info(f"[UART] Loopback synchronization verified in {latency}ms.")
                return ValidatorResult(
                    passed=True,
                    metrics={"uart_latency_ms": latency},
                    context=context_data
                )

            # 🚨 FORENSIC EMI / FRAMING INTERCEPTOR 🚨
            logger.critical("="*60)
            logger.critical(f"[UART] FATAL: GARBAGE DATA RECEIVED! (Framing Error / EMI)")
            logger.critical(f"[UART] Sent: {test_string}")
            logger.critical(f"[UART] Read: {response}")
            logger.critical(f"[UART] Raw Hex: {context_data['raw_rx_bytes']}")
            logger.critical(f"[UART] Check for baudrate mismatches, missing ground pins, or severe EMI noise.")
            logger.critical("="*60)
            return ValidatorResult(
                passed=False,
                error_msg=f"Garbage response received: '{response}' (Check baudrate/EMI)",
                context=context_data
            )

        except TransportTimeoutError:
            logger.critical("[UART] FATAL: DUT hung while configuring UART loopback. Kernel locked?")
            return ValidatorResult(passed=False, error_msg="DUT hung while configuring UART loopback.", context=context_data)
        except TransportConnectionError as e:
            logger.critical(f"[UART] FATAL: Transport pipe shattered during UART setup: {e}")
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
