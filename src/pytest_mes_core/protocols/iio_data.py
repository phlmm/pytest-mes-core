import structlog
import time
import logging
from typing import List, Dict, Any
from pytest_mes_core.transports import DutTransport, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
logger = structlog.get_logger('mes_core.protocols.iio')

class IioAdcValidator:
    """Validates physical Analog-to-Digital conversions via Linux IIO."""

    @staticmethod
    def measure_voltage(dut: DutTransport, iio_device_name: str, channel: int, samples: int=10, delay_s: float=0.01, min_v: float=0.0, max_v: float=5.0) -> ValidatorResult:
        """Measures an analog voltage by bursting reads over the Linux IIO subsystem.

        Args:
            dut: The transport interface connected to the target.
            iio_device_name: The name of the IIO device in sysfs.
            channel: The IIO channel number to read.
            samples: The number of samples to take.
            delay_s: The delay between samples in seconds.
            min_v: The minimum acceptable voltage.
            max_v: The maximum acceptable voltage.

        Returns:
            ValidatorResult: Pass/fail outcome based on voltage bounds.
        """
        context_data: Dict[str, Any] = {'target_sensor': iio_device_name, 'channel': channel}
        logger.info('sampling_voltage_samples_bursts_delay_s_s_delay', iio_device_name=iio_device_name, channel=channel, samples=samples, delay_s=delay_s)
        try:
            logger.debug('resolving_sysfs_path_for_iio_device_name_to_defeat_probe_order_races', iio_device_name=iio_device_name)
            resolve_cmd = f"grep -l '{iio_device_name}' /sys/bus/iio/devices/iio:device*/name 2>/dev/null"
            res_resolve = dut.safe_run(resolve_cmd, timeout_s=3.0)
            if not res_resolve.ok or not res_resolve.stdout:
                err_msg = f"Sensor '{iio_device_name}' not found in sysfs. Driver missing or probe failed?"
                logger.error('err_msg', err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg)
            base_path = res_resolve.stdout.strip().split('\n')[0].replace('/name', '')
            context_data['resolved_path'] = base_path
            logger.debug('interrogating_hardware_scale_mv_bit_from_base_path', iio_device_name=iio_device_name, channel=channel, base_path=base_path)
            res_scale = dut.safe_run(f'cat {base_path}/in_voltage{channel}_scale 2>/dev/null', timeout_s=2.0)
            if not res_scale.ok:
                err_msg = f'ADC Scale missing at {base_path}.'
                logger.error('err_msg', err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)
            scale = float(res_scale.stdout.strip())
            logger.debug('scale_scale_mv_bit_commencing_samples_sample_burst', iio_device_name=iio_device_name, channel=channel, scale=scale, samples=samples)
            bash_loop = f'for i in $(seq 1 {samples}); do cat {base_path}/in_voltage{channel}_raw; sleep {delay_s}; done'
            total_expected_time = samples * delay_s + 5.0
            res_raw = dut.safe_run(bash_loop, timeout_s=total_expected_time)
            if not res_raw.ok:
                logger.critical('fatal_kernel_rejected_raw_burst_read_stderr', iio_device_name=iio_device_name, channel=channel, stderr=res_raw.stderr)
                return ValidatorResult(passed=False, error_msg=f'Kernel rejected raw burst read: {res_raw.stderr}', context=context_data)
            raw_strings = res_raw.stdout.strip().split()
            if len(raw_strings) != samples:
                err_msg = f'Burst mismatch: Requested {samples}, got {len(raw_strings)}'
                logger.error('err_msg', err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)
            raw_values = [int(val) for val in raw_strings]
            avg_raw = sum(raw_values) / len(raw_values)
            voltage_v = round(avg_raw * scale / 1000.0, 4)
            context_data['raw_matrix'] = raw_values
            passed = min_v <= voltage_v <= max_v
            if not passed:
                logger.critical('=' * 60)
                logger.critical('fatal_voltage_out_of_bounds', iio_device_name=iio_device_name, channel=channel)
                logger.critical('measured_voltage_v_v', iio_device_name=iio_device_name, channel=channel, voltage_v=voltage_v)
                logger.critical('required_min_v_v_max_v_v', iio_device_name=iio_device_name, channel=channel, min_v=min_v, max_v=max_v)
                logger.critical('[ADC] Check physical power rails, sensor wiring, or for a short-to-ground.')
                logger.critical('=' * 60)
            else:
                logger.info('averaged_avg_raw_raw_voltage_v_v_passed', iio_device_name=iio_device_name, channel=channel, avg_raw=avg_raw, voltage_v=voltage_v)
            return ValidatorResult(passed=passed, metrics={'voltage_v': voltage_v, 't_sampling_s': res_raw.duration_s}, error_msg='' if passed else f'Voltage {voltage_v}V out of bounds', context=context_data)
        except ValueError as e:
            logger.error('failed_to_parse_numeric_iio_data_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Failed to parse numeric IIO data: {e}', context=context_data)
        except TransportTimeoutError:
            logger.critical('fatal_dut_hung_during_adc_sampling_burst_i2c_spi_bus_lockup')
            return ValidatorResult(passed=False, error_msg='DUT hung during ADC sampling burst.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_transport_pipe_shattered_during_adc_read_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Transport pipe shattered during ADC read: {e}', context=context_data)

class IioDacActuator:
    """Controls physical Digital-to-Analog hardware outputs via Linux IIO."""

    @staticmethod
    def set_voltage(dut: DutTransport, iio_device_name: str, channel: int, target_v: float) -> ValidatorResult:
        """Sets an analog voltage output via the Linux IIO subsystem.

        Args:
            dut: The transport interface connected to the target.
            iio_device_name: The name of the IIO DAC device in sysfs.
            channel: The IIO DAC channel number to write to.
            target_v: The target voltage to set.

        Returns:
            ValidatorResult: Pass/fail outcome of the DAC set operation.
        """
        context_data: Dict[str, Any] = {'target_v': target_v}
        logger.info('requesting_target_target_v_v', iio_device_name=iio_device_name, channel=channel, target_v=target_v)
        try:
            logger.debug('resolving_sysfs_path_for_iio_device_name', iio_device_name=iio_device_name)
            resolve_cmd = f"grep -l '{iio_device_name}' /sys/bus/iio/devices/iio:device*/name 2>/dev/null"
            res_resolve = dut.safe_run(resolve_cmd, timeout_s=3.0)
            if not res_resolve.ok or not res_resolve.stdout:
                err_msg = f"DAC '{iio_device_name}' not found in sysfs."
                logger.error('err_msg', err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg)
            base_path = res_resolve.stdout.strip().split('\n')[0].replace('/name', '')
            logger.debug('interrogating_hardware_scale', iio_device_name=iio_device_name, channel=channel)
            res_scale = dut.safe_run(f'cat {base_path}/out_voltage{channel}_scale 2>/dev/null', timeout_s=2.0)
            if not res_scale.ok:
                err_msg = f'DAC Scale missing: {base_path}'
                logger.error('err_msg', err_msg=err_msg)
                return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)
            scale = float(res_scale.stdout.strip())
            raw_val = int(target_v * 1000.0 / scale)
            context_data['quantized_raw'] = raw_val
            logger.debug('quantized_target_v_v_to_raw_raw_val_scale_scale', iio_device_name=iio_device_name, channel=channel, target_v=target_v, raw_val=raw_val, scale=scale)
            logger.debug('committing_raw_value_to_silicon', iio_device_name=iio_device_name, channel=channel)
            res_write = dut.safe_run(f'echo {raw_val} > {base_path}/out_voltage{channel}_raw', timeout_s=3.0)
            if not res_write.ok:
                logger.critical('=' * 60)
                logger.critical('fatal_silicon_rejected_dac_write_operation', iio_device_name=iio_device_name, channel=channel)
                logger.critical('kernel_stderr_val', val=res_write.stderr.strip())
                logger.critical('=' * 60)
                return ValidatorResult(passed=False, error_msg='Failed to write DAC raw register.', context=context_data)
            logger.info('successfully_drove_target_v_v', iio_device_name=iio_device_name, channel=channel, target_v=target_v)
            return ValidatorResult(passed=True, metrics={'t_dac_set_s': res_write.duration_s}, context=context_data)
        except TransportTimeoutError:
            logger.critical('fatal_i2c_spi_bus_locked_up_while_setting_dac')
            return ValidatorResult(passed=False, error_msg='I2C/SPI Bus locked up while setting DAC.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_transport_dropped_during_dac_write_e', e=e)
            return ValidatorResult(passed=False, error_msg=f'Transport dropped during DAC write: {e}', context=context_data)