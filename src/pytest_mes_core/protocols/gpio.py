# src/pytest_mes_core/protocols/gpio.py
import time
import logging
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import GpioEdgeConfig, GpioLedConfig, GpioLoopbackConfig

logger = logging.getLogger("mes_core.protocols.gpio")

class GpioEdgeValidator:
    """Validates physical buttons or hardware interrupt signals."""

    @staticmethod
    def _execute_gpiomon_trap(dut: DutTransport, cfg: GpioEdgeConfig, trap_name: str) -> ValidatorResult:
        """Internal physical execution engine for edge traps."""
        if cfg.edge_type not in ["rising-edge", "falling-edge", "both-edges"]:
            raise ValueError(f"FATAL: Invalid edge_type '{cfg.edge_type}' in TOML configuration.")

        cmd = f"gpiomon --num-events=1 --{cfg.edge_type} gpiochip{cfg.gpiochip} {cfg.line}"

        # Elevated to WARNING so the factory operator sees that the system is waiting for physical action
        logger.warning(f"[{trap_name} {cfg.gpiochip}:{cfg.line}] Arming {cfg.edge_type} hardware trap (Timeout: {cfg.timeout_s}s)...")
        logger.debug(f"[{trap_name}] Executing background listener: {cmd}")

        try:
            res = dut.safe_run(cmd, timeout_s=cfg.timeout_s)

            if res.exited != 0:
                logger.error(f"[{trap_name} {cfg.gpiochip}:{cfg.line}] Edge not detected (Exit Code {res.exited}).")
                return ValidatorResult(
                    passed=False,
                    error_msg=f"{trap_name} command failed: {res.stderr.strip()}",
                    metrics={"t_edge_response_s": -1.0}
                )

            logger.info(f"[{trap_name} {cfg.gpiochip}:{cfg.line}] Edge trap sprung successfully in {res.duration_s}s.")
            return ValidatorResult(passed=True, metrics={"t_edge_response_s": res.duration_s})

        except TransportTimeoutError:
            logger.error(f"[{trap_name} {cfg.gpiochip}:{cfg.line}] TIMEOUT. No edge occurred within {cfg.timeout_s}s.")
            return ValidatorResult(
                passed=False,
                error_msg=f"{trap_name} timeout ({cfg.timeout_s}s). No interrupt detected.",
                metrics={"t_edge_response_s": -1.0}
            )
        except TransportConnectionError as e:
            logger.critical("="*60)
            logger.critical(f"[{trap_name}] FATAL: Transport pipe shattered while awaiting hardware edge!")
            logger.critical("="*60)
            return ValidatorResult(passed=False, error_msg=f"Transport pipe shattered while awaiting edge: {e}")

    @staticmethod
    def verify_button_press(dut: DutTransport, cfg: GpioEdgeConfig) -> ValidatorResult:
        """Use this for human-interactive buttons.

        Arms a hardware trap and waits for a physical edge transition.

        Args:
            dut: The transport interface connected to the target.
            cfg: The GPIO edge configuration.

        Returns:
            ValidatorResult: Pass/fail outcome and the time it took to detect the edge.
        """
        return GpioEdgeValidator._execute_gpiomon_trap(dut, cfg, trap_name="GPIO Button")

    @staticmethod
    def await_interrupt_pulse(dut: DutTransport, cfg: GpioEdgeConfig) -> ValidatorResult:
        """Use this for silicon-driven IRQ lines (e.g., PIC18 INT_SOM#).

        Arms a hardware trap to detect high-speed interrupts from auxiliary silicon.

        Args:
            dut: The transport interface connected to the target.
            cfg: The GPIO edge configuration.

        Returns:
            ValidatorResult: Pass/fail outcome and the time it took to detect the interrupt.
        """
        return GpioEdgeValidator._execute_gpiomon_trap(dut, cfg, trap_name="GPIO IRQ")


class GpioLedActuator:
    """Manages diagnostic LEDs or optical optocoupler outputs."""

    @staticmethod
    def set_output(dut: DutTransport, cfg: GpioLedConfig, state: bool) -> None:
        """Drives a GPIO line high or low.
        
        Uses background mode to hold the line state after the command exits.

        Args:
            dut: The transport interface connected to the target.
            cfg: The GPIO output configuration.
            state: True to drive the line HIGH (1), False for LOW (0).
        """
        val = 1 if state else 0
        logger.debug(f"[GPIO {cfg.gpiochip}:{cfg.line}] Driving line to {val} (Background mode)...")

        # DEFENSIVE: >/dev/null 2>&1 prevents transport hanging on the open POSIX pipes
        cmd = f"gpioset --mode=wait gpiochip{cfg.gpiochip} {cfg.line}={val} >/dev/null 2>&1 &"

        try:
            # We use safe_run to maintain transport agnosticism.
            # Because it's backgrounded ('&'), it should return immediately.
            dut.safe_run(cmd, timeout_s=2.0)
        except TransportConnectionError:
            logger.warning("[GPIO] Failed to set output: Transport disconnected.")

    @staticmethod
    def teardown_zero_leakage(dut: DutTransport) -> None:
        """ZERO-LEAKAGE: Kills all lingering gpioset holds to turn off all outputs.

        Args:
            dut: The transport interface connected to the target.
        """
        logger.debug("[GPIO] ZERO-LEAKAGE: Releasing all active gpioset line holds.")
        try:
            dut.safe_run("killall -9 gpioset >/dev/null 2>&1 || true", timeout_s=3.0)
        except TransportConnectionError:
            pass # Ignore if the board is already rebooting/dead


class GpioLoopbackValidator:
    """Validates digital I/O pairs, accounting for optocoupler and trace propagation delay."""

    @staticmethod
    def verify_loopback(dut: DutTransport, cfg: GpioLoopbackConfig, test_state: bool = True) -> ValidatorResult:
        """Validates physical signal propagation across a GPIO loopback pair.

        Args:
            dut: The transport interface connected to the target.
            cfg: The GPIO loopback configuration.
            test_state: The boolean state to drive on the TX line.

        Returns:
            ValidatorResult: Pass/fail outcome indicating whether RX matched TX.
        """
        tx_val = 1 if test_state else 0
        logger.info(f"[GPIO Loopback] Testing TX {cfg.tx_gpiochip}:{cfg.tx_line} -> RX {cfg.rx_gpiochip}:{cfg.rx_line} (State: {tx_val})")

        pid_file = f"/tmp/mes_gpioset_{cfg.tx_gpiochip}_{cfg.tx_line}.pid"
        context_data: Dict[str, Any] = {"tx_val": tx_val}

        # DEFENSIVE: >/dev/null 2>&1 prevents transport hangs, and echo $! captures the PID securely
        tx_cmd = (
            f"gpioset --mode=wait gpiochip{cfg.tx_gpiochip} {cfg.tx_line}={tx_val} >/dev/null 2>&1 & "
            f"echo $! > {pid_file}"
        )

        try:
            logger.debug(f"[GPIO Loopback] Driving TX line to {tx_val}...")
            dut.safe_run(tx_cmd, timeout_s=3.0)

            logger.debug(f"[GPIO Loopback] Allowing {cfg.settling_time_s}s for hardware physics/optocouplers to settle...")
            time.sleep(cfg.settling_time_s)

            rx_cmd = f"gpioget gpiochip{cfg.rx_gpiochip} {cfg.rx_line}"
            logger.debug(f"[GPIO Loopback] Sampling RX line...")
            res_rx = dut.safe_run(rx_cmd, timeout_s=5.0)

            if not res_rx.ok:
                logger.error(f"[GPIO Loopback] Failed to read RX line: {res_rx.stderr.strip()}")
                return ValidatorResult(passed=False, error_msg=f"gpioget execution failed: {res_rx.stderr.strip()}")

            try:
                rx_val = int(res_rx.stdout.strip())
                context_data["rx_val"] = rx_val
            except ValueError:
                return ValidatorResult(passed=False, error_msg=f"Invalid gpioget output: {res_rx.stdout}", context=context_data)

            passed = (rx_val == tx_val)
            if not passed:
                #  FORENSIC HARDWARE INTERCEPTOR
                logger.critical("="*60)
                logger.critical(f"[GPIO Loopback] FATAL: HARDWARE MISMATCH DETECTED!")
                logger.critical(f"[GPIO Loopback] TX was driven to {tx_val}, but RX sampled {rx_val}.")
                logger.critical("[GPIO Loopback] Possible PCB short, broken trace, or dead optocoupler.")
                logger.critical("="*60)
            else:
                logger.info("[GPIO Loopback] Physical signal propagation verified successfully.")

            return ValidatorResult(
                passed=passed,
                context=context_data,
                error_msg="" if passed else f"Loopback mismatch (TX:{tx_val} RX:{rx_val})"
            )

        except TransportTimeoutError:
            logger.critical("[GPIO Loopback] FATAL: DUT hung during GPIO loopback verification. Kernel locked?")
            return ValidatorResult(passed=False, error_msg="DUT hung during GPIO loopback verification.", context=context_data)
        except TransportConnectionError as e:
            logger.critical(f"[GPIO Loopback] FATAL: Transport dropped during GPIO loopback: {e}")
            return ValidatorResult(passed=False, error_msg=f"Transport dropped during GPIO loopback: {e}", context=context_data)

        finally:
            # ZERO-LEAKAGE: Surgically kill only this specific TX hold
            if dut.is_connected:
                try:
                    logger.debug(f"[GPIO Loopback] ZERO-LEAKAGE: Releasing surgical TX line hold (PID from {pid_file}).")
                    dut.safe_run(f"kill -9 $(cat {pid_file} 2>/dev/null) >/dev/null 2>&1 || true", timeout_s=3.0)
                    dut.safe_run(f"rm -f {pid_file} >/dev/null 2>&1 || true", timeout_s=3.0)
                except Exception as cleanup_err:
                    logger.debug(f"[GPIO Loopback] Teardown failed (Transport likely dead): {cleanup_err}")
