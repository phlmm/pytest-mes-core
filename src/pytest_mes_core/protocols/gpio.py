import structlog
import time
import logging
from typing import Dict, Any
from pytest_mes_core.transports import DutTransport, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import GpioEdgeConfig, GpioLedConfig, GpioLoopbackConfig
logger = structlog.get_logger('mes_core.protocols.gpio')

class GpioEdgeValidator:
    """Validates physical buttons or hardware interrupt signals."""

    @staticmethod
    def _execute_gpiomon_trap(dut: DutTransport, cfg: GpioEdgeConfig, trap_name: str) -> ValidatorResult:
        """Internal physical execution engine for edge traps."""
        if cfg.edge_type not in ['rising-edge', 'falling-edge', 'both-edges']:
            raise ValueError(f"FATAL: Invalid edge_type '{cfg.edge_type}' in TOML configuration.")
        cmd = f'gpiomon --num-events=1 --{cfg.edge_type} gpiochip{cfg.gpiochip} {cfg.line}'
        logger.warning('arming_edge_type_hardware_trap_timeout_timeout_s_s', trap_name=trap_name, gpiochip=cfg.gpiochip, line=cfg.line, edge_type=cfg.edge_type, timeout_s=cfg.timeout_s)
        logger.debug('executing_background_listener_cmd', trap_name=trap_name, cmd=cmd)
        try:
            res = dut.safe_run(cmd, timeout_s=cfg.timeout_s)
            if res.exited != 0:
                logger.error('edge_not_detected_exit_code_exited', trap_name=trap_name, gpiochip=cfg.gpiochip, line=cfg.line, exited=res.exited)
                return ValidatorResult(passed=False, error_msg=f'{trap_name} command failed: {res.stderr.strip()}', metrics={'t_edge_response_s': -1.0})
            logger.info('edge_trap_sprung_successfully_in_duration_s_s', trap_name=trap_name, gpiochip=cfg.gpiochip, line=cfg.line, duration_s=res.duration_s)
            return ValidatorResult(passed=True, metrics={'t_edge_response_s': res.duration_s})
        except TransportTimeoutError:
            logger.error('timeout_no_edge_occurred_within_timeout_s_s', trap_name=trap_name, gpiochip=cfg.gpiochip, line=cfg.line, timeout_s=cfg.timeout_s)
            return ValidatorResult(passed=False, error_msg=f'{trap_name} timeout ({cfg.timeout_s}s). No interrupt detected.', metrics={'t_edge_response_s': -1.0})
        except TransportConnectionError as e:
            logger.critical('=' * 60)
            logger.critical('fatal_transport_pipe_shattered_while_awaiting_hardware_edge', trap_name=trap_name)
            logger.critical('=' * 60)
            return ValidatorResult(passed=False, error_msg=f'Transport pipe shattered while awaiting edge: {e}')

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
        return GpioEdgeValidator._execute_gpiomon_trap(dut, cfg, trap_name='GPIO Button')

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
        return GpioEdgeValidator._execute_gpiomon_trap(dut, cfg, trap_name='GPIO IRQ')

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
        logger.debug('driving_line_to_val_background_mode', gpiochip=cfg.gpiochip, line=cfg.line, val=val)
        cmd = f'gpioset --mode=wait gpiochip{cfg.gpiochip} {cfg.line}={val} >/dev/null 2>&1 &'
        try:
            dut.safe_run(cmd, timeout_s=2.0)
        except TransportConnectionError:
            logger.warning('[GPIO] Failed to set output: Transport disconnected.')

    @staticmethod
    def teardown_zero_leakage(dut: DutTransport) -> None:
        """ZERO-LEAKAGE: Kills all lingering gpioset holds to turn off all outputs.

        Args:
            dut: The transport interface connected to the target.
        """
        logger.debug('[GPIO] ZERO-LEAKAGE: Releasing all active gpioset line holds.')
        try:
            dut.safe_run('killall -9 gpioset >/dev/null 2>&1 || true', timeout_s=3.0)
        except TransportConnectionError:
            pass

class GpioLoopbackValidator:
    """Validates digital I/O pairs, accounting for optocoupler and trace propagation delay."""

    @staticmethod
    def verify_loopback(dut: DutTransport, cfg: GpioLoopbackConfig, test_state: bool=True) -> ValidatorResult:
        """Validates physical signal propagation across a GPIO loopback pair.

        Args:
            dut: The transport interface connected to the target.
            cfg: The GPIO loopback configuration.
            test_state: The boolean state to drive on the TX line.

        Returns:
            ValidatorResult: Pass/fail outcome indicating whether RX matched TX.
        """
        tx_val = 1 if test_state else 0
        logger.info('testing_tx_tx_gpiochip_tx_line_rx_rx_gpiochip_rx_line_state_tx_val', tx_gpiochip=cfg.tx_gpiochip, tx_line=cfg.tx_line, rx_gpiochip=cfg.rx_gpiochip, rx_line=cfg.rx_line, tx_val=tx_val)
        pid_file = f'/tmp/mes_gpioset_{cfg.tx_gpiochip}_{cfg.tx_line}.pid'
        context_data: Dict[str, Any] = {'tx_val': tx_val}
        tx_cmd = f'gpioset --mode=wait gpiochip{cfg.tx_gpiochip} {cfg.tx_line}={tx_val} >/dev/null 2>&1 & echo $! > {pid_file}'
        try:
            logger.debug('driving_tx_line_to_tx_val', tx_val=tx_val)
            dut.safe_run(tx_cmd, timeout_s=3.0)
            logger.debug('allowing_settling_time_s_s_for_hardware_physics_optocouplers_to_settle', settling_time_s=cfg.settling_time_s)
            time.sleep(cfg.settling_time_s)
            rx_cmd = f'gpioget gpiochip{cfg.rx_gpiochip} {cfg.rx_line}'
            logger.debug('sampling_rx_line')
            res_rx = dut.safe_run(rx_cmd, timeout_s=5.0)
            if not res_rx.ok:
                logger.error('failed_to_read_rx_line_val', val=res_rx.stderr.strip())
                return ValidatorResult(passed=False, error_msg=f'gpioget execution failed: {res_rx.stderr.strip()}')
            try:
                rx_val = int(res_rx.stdout.strip())
                context_data['rx_val'] = rx_val
            except ValueError:
                return ValidatorResult(passed=False, error_msg=f'Invalid gpioget output: {res_rx.stdout}', context=context_data)
            passed = rx_val == tx_val
            if not passed:
                logger.critical('=' * 60)
                logger.critical('fatal_hardware_mismatch_detected')
                logger.critical('tx_was_driven_to_tx_val_but_rx_sampled_rx_val', tx_val=tx_val, rx_val=rx_val)
                logger.critical('[GPIO Loopback] Possible PCB short, broken trace, or dead optocoupler.')
                logger.critical('=' * 60)
            else:
                logger.info('[GPIO Loopback] Physical signal propagation verified successfully.')
            return ValidatorResult(passed=passed, context=context_data, error_msg='' if passed else f'Loopback mismatch (TX:{tx_val} RX:{rx_val})')
        except TransportTimeoutError:
            logger.critical('[GPIO Loopback] FATAL: DUT hung during GPIO loopback verification. Kernel locked?')
            return ValidatorResult(passed=False, error_msg='DUT hung during GPIO loopback verification.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_transport_dropped_during_gpio_loopback_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Transport dropped during GPIO loopback: {e}', context=context_data)
        finally:
            if dut.is_connected:
                try:
                    logger.debug('zero_leakage_releasing_surgical_tx_line_hold_pid_from_pid_file', pid_file=pid_file)
                    dut.safe_run(f'kill -9 $(cat {pid_file} 2>/dev/null) >/dev/null 2>&1 || true', timeout_s=3.0)
                    dut.safe_run(f'rm -f {pid_file} >/dev/null 2>&1 || true', timeout_s=3.0)
                except Exception as cleanup_err:
                    logger.debug('teardown_failed_transport_likely_dead_cleanup_err', cleanup_err=cleanup_err)