# src/pytest_mes_core/protocols/iio_data.py
import time
import logging
from typing import List, Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult

logger = logging.getLogger("mes_core.protocols.iio")

class IioAdcValidator:
    """Validates physical Analog-to-Digital conversions via Linux IIO."""

    @staticmethod
    def measure_voltage(
        dut: DutTransport,
        iio_device_name: str, # Upgraded from int to str to defeat probe-order races
        channel: int,
        samples: int = 10,
        delay_s: float = 0.01,
        min_v: float = 0.0,
        max_v: float = 5.0
    ) -> ValidatorResult:

        context_data: Dict[str, Any] = {"target_sensor": iio_device_name, "channel": channel}
        logger.info(f"[ADC {iio_device_name}:{channel}] Sampling voltage ({samples} bursts, {delay_s}s delay)...")

        try:
            # 1. Dynamic Device Resolution (Defeats Probe-Order Race Conditions)
            logger.debug(f"[ADC] Resolving sysfs path for '{iio_device_name}' to defeat probe-order races...")
            resolve_cmd = f"grep -l '{iio_device_name}' /sys/bus/iio/devices/iio:device*/name 2>/dev/null"
            res_resolve = dut.safe_run(resolve_cmd, timeout_s=3.0)

            if not res_resolve.ok or not res_resolve.stdout:
                err_msg = f"Sensor '{iio_device_name}' not found in sysfs. Driver missing or probe failed?"
                logger.error(f"[ADC] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg)

            base_path = res_resolve.stdout.strip().split('\n')[0].replace("/name", "")
            context_data["resolved_path"] = base_path

            # 2. Scale Resolution
            logger.debug(f"[ADC {iio_device_name}:{channel}] Interrogating hardware scale (mV/bit) from {base_path}...")
            res_scale = dut.safe_run(f"cat {base_path}/in_voltage{channel}_scale 2>/dev/null", timeout_s=2.0)
            if not res_scale.ok:
                err_msg = f"ADC Scale missing at {base_path}."
                logger.error(f"[ADC] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)

            scale = float(res_scale.stdout.strip())
            logger.debug(f"[ADC {iio_device_name}:{channel}] Scale = {scale} mV/bit. Commencing {samples}-sample burst...")

            # 3. SILICON-SIDE BATCH EXECUTION (Zero Network Latency)
            # We construct a pure POSIX shell loop to execute directly on the DUT CPU.
            # This guarantees the `delay_s` is perfectly accurate without SSH/UART overhead.
            bash_loop = (
                f"for i in $(seq 1 {samples}); do "
                f"cat {base_path}/in_voltage{channel}_raw; "
                f"sleep {delay_s}; "
                f"done"
            )

            # Add a generous timeout to account for the total intended sleep duration
            total_expected_time = (samples * delay_s) + 5.0
            res_raw = dut.safe_run(bash_loop, timeout_s=total_expected_time)

            if not res_raw.ok:
                logger.critical(f"[ADC {iio_device_name}:{channel}] FATAL: Kernel rejected raw burst read: {res_raw.stderr}")
                return ValidatorResult(passed=False, error_msg=f"Kernel rejected raw burst read: {res_raw.stderr}", context=context_data)

            # 4. Data Processing
            raw_strings = res_raw.stdout.strip().split()
            if len(raw_strings) != samples:
                err_msg = f"Burst mismatch: Requested {samples}, got {len(raw_strings)}"
                logger.error(f"[ADC] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)

            raw_values = [int(val) for val in raw_strings]
            avg_raw = sum(raw_values) / len(raw_values)
            voltage_v = round((avg_raw * scale) / 1000.0, 4)

            context_data["raw_matrix"] = raw_values

            # 5. Logical Evaluation
            passed = min_v <= voltage_v <= max_v
            if not passed:
                #  FORENSIC HARDWARE INTERCEPTOR
                logger.critical("="*60)
                logger.critical(f"[ADC {iio_device_name}:{channel}] FATAL: VOLTAGE OUT OF BOUNDS!")
                logger.critical(f"[ADC {iio_device_name}:{channel}] Measured: {voltage_v} V")
                logger.critical(f"[ADC {iio_device_name}:{channel}] Required: [{min_v} V, {max_v} V]")
                logger.critical("[ADC] Check physical power rails, sensor wiring, or for a short-to-ground.")
                logger.critical("="*60)
            else:
                logger.info(f"[ADC {iio_device_name}:{channel}] Averaged {avg_raw:.1f} raw -> {voltage_v} V (Passed)")

            return ValidatorResult(
                passed=passed,
                metrics={"voltage_v": voltage_v, "t_sampling_s": res_raw.duration_s},
                error_msg="" if passed else f"Voltage {voltage_v}V out of bounds",
                context=context_data
            )

        except ValueError as e:
            logger.error(f"[ADC] Failed to parse numeric IIO data: {e}")
            return ValidatorResult(passed=False, error_msg=f"Failed to parse numeric IIO data: {e}", context=context_data)
        except TransportTimeoutError:
            logger.critical(f"[ADC] FATAL: DUT hung during ADC sampling burst! I2C/SPI bus lockup?")
            return ValidatorResult(passed=False, error_msg="DUT hung during ADC sampling burst.", context=context_data)
        except TransportConnectionError as e:
            logger.critical(f"[ADC] FATAL: Transport pipe shattered during ADC read: {e}")
            return ValidatorResult(passed=False, error_msg=f"Transport pipe shattered during ADC read: {e}", context=context_data)


class IioDacActuator:
    """Controls physical Digital-to-Analog hardware outputs via Linux IIO."""

    @staticmethod
    def set_voltage(
        dut: DutTransport,
        iio_device_name: str,
        channel: int,
        target_v: float
    ) -> ValidatorResult:

        context_data: Dict[str, Any] = {"target_v": target_v}
        logger.info(f"[DAC {iio_device_name}:{channel}] Requesting target {target_v} V...")

        try:
            # 1. Dynamic Device Resolution
            logger.debug(f"[DAC] Resolving sysfs path for '{iio_device_name}'...")
            resolve_cmd = f"grep -l '{iio_device_name}' /sys/bus/iio/devices/iio:device*/name 2>/dev/null"
            res_resolve = dut.safe_run(resolve_cmd, timeout_s=3.0)

            if not res_resolve.ok or not res_resolve.stdout:
                err_msg = f"DAC '{iio_device_name}' not found in sysfs."
                logger.error(f"[DAC] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg)

            base_path = res_resolve.stdout.strip().split('\n')[0].replace("/name", "")

            # 2. Resolve Scale
            logger.debug(f"[DAC {iio_device_name}:{channel}] Interrogating hardware scale...")
            res_scale = dut.safe_run(f"cat {base_path}/out_voltage{channel}_scale 2>/dev/null", timeout_s=2.0)
            if not res_scale.ok:
                err_msg = f"DAC Scale missing: {base_path}"
                logger.error(f"[DAC] {err_msg}")
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)

            scale = float(res_scale.stdout.strip())
            raw_val = int((target_v * 1000.0) / scale)
            context_data["quantized_raw"] = raw_val

            logger.debug(f"[DAC {iio_device_name}:{channel}] Quantized {target_v}V to raw {raw_val} (Scale: {scale})")

            # 3. Hardware Write
            logger.debug(f"[DAC {iio_device_name}:{channel}] Committing raw value to silicon...")
            res_write = dut.safe_run(f"echo {raw_val} > {base_path}/out_voltage{channel}_raw", timeout_s=3.0)

            if not res_write.ok:
                logger.critical("="*60)
                logger.critical(f"[DAC {iio_device_name}:{channel}] FATAL: Silicon rejected DAC write operation!")
                logger.critical(f"[DAC] Kernel Stderr: {res_write.stderr.strip()}")
                logger.critical("="*60)
                return ValidatorResult(passed=False, error_msg="Failed to write DAC raw register.", context=context_data)

            logger.info(f"[DAC {iio_device_name}:{channel}] Successfully drove {target_v} V.")
            return ValidatorResult(
                passed=True,
                metrics={"t_dac_set_s": res_write.duration_s},
                context=context_data
            )

        except TransportTimeoutError:
            logger.critical(f"[DAC] FATAL: I2C/SPI Bus locked up while setting DAC!")
            return ValidatorResult(passed=False, error_msg="I2C/SPI Bus locked up while setting DAC.", context=context_data)
        except TransportConnectionError as e:
            logger.critical(f"[DAC] FATAL: Transport dropped during DAC write: {e}")
            return ValidatorResult(passed=False, error_msg=f"Transport dropped during DAC write: {e}", context=context_data)
