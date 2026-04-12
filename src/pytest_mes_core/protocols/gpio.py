import time
import logging
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult
from pytest_mes_core.config import GpioEdgeConfig, GpioLedConfig, GpioLoopbackConfig

logger = logging.getLogger("mes_core.protocols.gpio")

class GpioEdgeValidator:
    """Validates physical buttons or hardware interrupt signals."""

    @staticmethod
    def _execute_gpiomon_trap(dut_ssh: EphemeralSSHClient, cfg: GpioEdgeConfig, trap_name: str) -> ValidatorResult:
        """Internal physical execution engine for edge traps."""
        if cfg.edge_type not in ["rising-edge", "falling-edge", "both-edges"]:
            raise ValueError(f"FATAL: Invalid edge_type '{cfg.edge_type}' in TOML configuration.")

        cmd = f"gpiomon --num-events=1 --{cfg.edge_type} gpiochip{cfg.gpiochip} {cfg.line}"

        logger.info(f"[{trap_name} {cfg.gpiochip}:{cfg.line}] Arming {cfg.edge_type} trap (Timeout: {cfg.timeout_s}s)...")
        logger.debug(f"[{trap_name}] Executing: {cmd}")

        t0 = time.perf_counter()
        res = dut_ssh.safe_run(cmd, timeout_s=cfg.timeout_s) # Using the safe_run wrapper we built!
        duration = round(time.perf_counter() - t0, 3)

        if res.exited != 0:
            logger.error(f"[{trap_name} {cfg.gpiochip}:{cfg.line}] TIMEOUT. Edge not detected.")
            return ValidatorResult(
                passed=False,
                error_msg=f"{trap_name} timeout ({cfg.timeout_s}s).",
                metrics={"t_edge_response_s": -1.0}
            )

        logger.info(f"[{trap_name} {cfg.gpiochip}:{cfg.line}] Edge trap sprung in {duration}s.")
        return ValidatorResult(passed=True, metrics={"t_edge_response_s": duration})

    @staticmethod
    def verify_button_press(dut_ssh: EphemeralSSHClient, cfg: GpioEdgeConfig) -> ValidatorResult:
        """Use this for human-interactive buttons."""
        return GpioEdgeValidator._execute_gpiomon_trap(dut_ssh, cfg, trap_name="GPIO Button")

    @staticmethod
    def await_interrupt_pulse(dut_ssh: EphemeralSSHClient, cfg: GpioEdgeConfig) -> ValidatorResult:
        """Use this for silicon-driven IRQ lines (e.g., PIC18 INT_SOM#)."""
        return GpioEdgeValidator._execute_gpiomon_trap(dut_ssh, cfg, trap_name="GPIO IRQ")


class GpioLedActuator:
    """Manages diagnostic LEDs or optical optocoupler outputs."""

    @staticmethod
    def set_output(dut_ssh: EphemeralSSHClient, cfg: GpioLedConfig, state: bool) -> None:
        """
        Drives a GPIO line high or low.
        Uses background mode to hold the line state after the SSH command exits.
        """
        val = 1 if state else 0
        logger.debug(f"[GPIO {cfg.gpiochip}:{cfg.line}] Driving line to {val}")

        # DEFENSIVE: >/dev/null 2>&1 prevents OpenSSH from hanging on the open pipes!
        cmd = f"gpioset --mode=wait gpiochip{cfg.gpiochip} {cfg.line}={val} >/dev/null 2>&1 &"
        dut_ssh.conn.run(cmd, hide=True)

    @staticmethod
    def teardown_zero_leakage(dut_ssh: EphemeralSSHClient) -> None:
        """ZERO-LEAKAGE: Kills all lingering gpioset holds to turn off all outputs."""
        logger.debug("[GPIO] ZERO-LEAKAGE: Releasing all active gpioset line holds.")
        dut_ssh.conn.run("killall gpioset || true", hide=True, warn=True)


class GpioLoopbackValidator:
    """Validates digital I/O pairs, accounting for optocoupler and trace propagation delay."""

    @staticmethod
    def verify_loopback(dut_ssh: EphemeralSSHClient, cfg: GpioLoopbackConfig, test_state: bool = True) -> ValidatorResult:
        tx_val = 1 if test_state else 0
        logger.info(f"[GPIO Loopback] Testing TX {cfg.tx_gpiochip}:{cfg.tx_line} -> RX {cfg.rx_gpiochip}:{cfg.rx_line} (State: {tx_val})")

        pid_file = f"/tmp/mes_gpioset_{cfg.tx_gpiochip}_{cfg.tx_line}.pid"

        # DEFENSIVE: >/dev/null 2>&1 prevents SSH hangs, and echo $! captures the PID securely
        tx_cmd = (
            f"gpioset --mode=wait gpiochip{cfg.tx_gpiochip} {cfg.tx_line}={tx_val} >/dev/null 2>&1 & "
            f"echo $! > {pid_file}"
        )
        dut_ssh.conn.run(tx_cmd, hide=True)

        try:
            logger.debug(f"[GPIO Loopback] Allowing {cfg.settling_time_s}s for hardware physics to settle...")
            time.sleep(cfg.settling_time_s)

            rx_cmd = f"gpioget gpiochip{cfg.rx_gpiochip} {cfg.rx_line}"
            res_rx = dut_ssh.safe_run(rx_cmd, timeout_s=5.0)

            if not res_rx.ok:
                logger.error(f"[GPIO Loopback] Failed to read RX line: {res_rx.stderr.strip()}")
                return ValidatorResult(passed=False, error_msg="gpioget execution failed.")

            try:
                rx_val = int(res_rx.stdout.strip())
            except ValueError:
                return ValidatorResult(passed=False, error_msg=f"Invalid gpioget output: {res_rx.stdout}")

            passed = (rx_val == tx_val)
            if not passed:
                logger.warning(f"[GPIO Loopback] MISMATCH! TX driven to {tx_val}, but RX read {rx_val}.")
            else:
                logger.info("[GPIO Loopback] Signal propagation verified.")

            return ValidatorResult(
                passed=passed,
                context={"tx_val": tx_val, "rx_val": rx_val},
                error_msg="" if passed else f"Loopback mismatch (TX:{tx_val} RX:{rx_val})"
            )

        finally:
            # ZERO-LEAKAGE: Surgically kill only this specific TX hold
            logger.debug(f"[GPIO Loopback] ZERO-LEAKAGE: Releasing TX line hold.")
            dut_ssh.conn.run(f"kill -9 $(cat {pid_file} 2>/dev/null) || true", hide=True, warn=True)
            dut_ssh.conn.run(f"rm -f {pid_file}", hide=True, warn=True)
