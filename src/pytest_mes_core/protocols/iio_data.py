import time
import logging
from typing import List
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.protocols.iio")

class IioAdcValidator:
    @staticmethod
    def measure_voltage(
        dut_ssh: EphemeralSSHClient, iio_device: int, channel: int, samples: int = 10, delay_s: float = 0.01
    ) -> float:
        base_path = f"/sys/bus/iio/devices/iio:device{iio_device}"
        logger.debug(f"[ADC {iio_device}:{channel}] Resolving scale factor...")

        res_scale = dut_ssh.conn.run(f"cat {base_path}/in_voltage{channel}_scale", hide=True, warn=True)
        if not res_scale.ok:
            logger.error(f"[ADC {iio_device}:{channel}] Scale missing! Driver loaded?")
            raise RuntimeError(f"FATAL: ADC Scale missing at {base_path}.")

        scale = float(res_scale.stdout.strip())
        logger.debug(f"[ADC {iio_device}:{channel}] Scale = {scale} mV/bit. Bursting {samples} samples...")

        raw_values: List[int] = []
        for i in range(samples):
            res_raw = dut_ssh.conn.run(f"cat {base_path}/in_voltage{channel}_raw", hide=True)
            val = int(res_raw.stdout.strip())
            raw_values.append(val)
            time.sleep(delay_s)

        avg_raw = sum(raw_values) / len(raw_values)
        voltage_v = round((avg_raw * scale) / 1000.0, 4)

        logger.info(f"[ADC {iio_device}:{channel}] Averaged {avg_raw:.1f} raw -> {voltage_v} V")
        logger.debug(f"[ADC {iio_device}:{channel}] Raw burst matrix: {raw_values}")
        return voltage_v

class IioDacActuator:
    @staticmethod
    def set_voltage(dut_ssh: EphemeralSSHClient, iio_device: int, channel: int, target_v: float) -> ValidatorResult:
        base_path = f"/sys/bus/iio/devices/iio:device{iio_device}"
        logger.info(f"[DAC {iio_device}:{channel}] Requesting target {target_v} V")

        res_scale = dut_ssh.conn.run(f"cat {base_path}/out_voltage{channel}_scale", hide=True, warn=True)
        if not res_scale.ok:
            logger.error(f"[DAC {iio_device}:{channel}] Failed to read scale.")
            return ValidatorResult(passed=False, error_msg=f"DAC Scale missing: {base_path}")

        scale = float(res_scale.stdout.strip())
        raw_val = int((target_v * 1000.0) / scale)

        logger.debug(f"[DAC {iio_device}:{channel}] Quantized {target_v}V to raw {raw_val} (Scale: {scale})")
        res_write = dut_ssh.conn.run(f"echo {raw_val} > {base_path}/out_voltage{channel}_raw", hide=True, warn=True)

        if not res_write.ok:
            logger.error(f"[DAC {iio_device}:{channel}] Silicon rejected write operation.")
            return ValidatorResult(passed=False, error_msg="Failed to write DAC raw register.")

        return ValidatorResult(passed=True, context={"dac_target_v": target_v, "dac_raw": raw_val})
