import anyio
import structlog
import logging
from typing import Dict, Any
from pytest_mes_core.transports import DutTransport, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import MmioConfig
logger = structlog.get_logger('mes_core.protocols.mmio')

class MmioValidator:
    """
    Reads and writes raw physical silicon addresses, bypassing Linux kernel drivers.
    Features Data Abort/Kernel Panic interception and CONFIG_STRICT_DEVMEM detection.
    """

    @staticmethod
    def read_register(dut: DutTransport, cfg: MmioConfig) -> ValidatorResult:
        """Reads a raw physical silicon address bypassing the Linux kernel.

        Args:
            dut: The transport interface connected to the target.
            cfg: The MMIO configuration specifying address, width, mask, and expected value.

        Returns:
            ValidatorResult: Pass/fail outcome based on matching the expected masked value.
        """
        logger.info('reading_data_width_bit_hardware_register_at_address_hex', data_width=cfg.data_width, address_hex=cfg.address_hex)
        context_data: Dict[str, Any] = {'address': cfg.address_hex, 'mask': cfg.bit_mask_hex}
        cmd = f'devmem {cfg.address_hex} {cfg.data_width}'
        logger.debug('executing_kernel_bypass_cmd', cmd=cmd)
        try:
            res = dut.safe_run(cmd, timeout_s=3.0)
            if not res.ok:
                error_msg = res.stderr.strip() or res.stdout.strip()
                if 'Operation not permitted' in error_msg:
                    logger.critical('=' * 60)
                    logger.critical('[MMIO] FATAL: KERNEL BLOCKED PHYSICAL MEMORY ACCESS!')
                    logger.critical('[MMIO] CONFIG_STRICT_DEVMEM is enabled in the Linux kernel.')
                    logger.critical('[MMIO] You must disable it in the kernel defconfig to use this validator.')
                    logger.critical('=' * 60)
                    return ValidatorResult(passed=False, error_msg='Kernel blocked access (CONFIG_STRICT_DEVMEM enabled).', context=context_data)
                logger.error('failed_to_read_address_hex_error_msg', address_hex=cfg.address_hex, error_msg=error_msg)
                return ValidatorResult(passed=False, error_msg=f'MMIO Read Failed: {error_msg}', context=context_data)
            raw_val = int(res.stdout.strip(), 16)
            mask = int(cfg.bit_mask_hex, 16)
            masked_val = raw_val & mask
            hex_result = hex(masked_val).upper()
            context_data['mmio_raw_val'] = hex(raw_val).upper()
            context_data['mmio_masked_val'] = hex_result
            logger.debug('raw_val_mask_bit_mask_hex_result_hex_result', val=context_data['mmio_raw_val'], bit_mask_hex=cfg.bit_mask_hex, hex_result=hex_result)
            if cfg.expected_value_hex:
                expected = int(cfg.expected_value_hex, 16)
                if masked_val != expected:
                    logger.warning('mismatch_expected_val_got_hex_result', val=hex(expected).upper(), hex_result=hex_result)
                    return ValidatorResult(passed=False, error_msg=f'MMIO Mismatch (Expected {hex(expected).upper()}, Got {hex_result})', metrics={'t_mmio_read_s': res.duration_s}, context=context_data)
            logger.info('register_evaluation_passed_hex_result', hex_result=hex_result)
            return ValidatorResult(passed=True, metrics={'t_mmio_read_s': res.duration_s}, context=context_data)
        except ValueError as e:
            logger.error('invalid_devmem_output_val', val=res.stdout.strip())
            return ValidatorResult(passed=False, error_msg=f'Failed to parse devmem output: {e}', context=context_data)
        except TransportTimeoutError:
            logger.critical('=' * 60)
            logger.critical('fatal_axi_ahb_bus_hang_detected')
            logger.critical('silicon_locked_up_reading_address_hex', address_hex=cfg.address_hex)
            logger.critical('[MMIO] Target peripheral clock is likely disabled (unclocked domain).')
            logger.critical('=' * 60)
            return ValidatorResult(passed=False, error_msg='Bus Hang: Silicon locked up (Unclocked domain?).', context=context_data)
        except TransportConnectionError as e:
            logger.critical('=' * 60)
            logger.critical('fatal_kernel_panic_data_abort_detected')
            logger.critical('hardware_aggressively_rejected_access_to_address_hex_e', address_hex=cfg.address_hex, e=e)
            logger.critical('[MMIO] You are likely reading unmapped or protected TrustZone memory.')
            logger.critical('=' * 60)
            return ValidatorResult(passed=False, error_msg=f'Kernel Panic/Data Abort accessing MMIO: {e}', context=context_data)


    @staticmethod
    def write_register(dut: DutTransport, cfg: MmioConfig, write_val_hex: str) -> ValidatorResult:
        """Writes raw bits directly to physical silicon bypassing the Linux kernel.

        DANGEROUS: Can cause immediate hard-faults if written to read-only/protected memory.

        Args:
            dut: The transport interface connected to the target.
            cfg: The MMIO configuration specifying address and width.
            write_val_hex: The hex string value to write.

        Returns:
            ValidatorResult: Pass/fail outcome of the raw write operation.
        """
        context_data: Dict[str, Any] = {'address': cfg.address_hex, 'write_val': write_val_hex}
        logger.warning('=' * 60)
        logger.warning('danger_forcing_hardware_write_to_address_hex', address_hex=cfg.address_hex)
        logger.warning('injecting_value_write_val_hex_data_width_bit', write_val_hex=write_val_hex, data_width=cfg.data_width)
        logger.warning('=' * 60)
        cmd = f'devmem {cfg.address_hex} {cfg.data_width} {write_val_hex}'
        logger.debug('executing_kernel_bypass_cmd', cmd=cmd)
        try:
            res = dut.safe_run(cmd, timeout_s=3.0)
            if not res.ok:
                error_msg = res.stderr.strip() or res.stdout.strip()
                if 'Operation not permitted' in error_msg:
                    logger.critical('fatal_kernel_blocked_access_config_strict_devmem_is_enabled')
                    return ValidatorResult(passed=False, error_msg='Kernel blocked write (CONFIG_STRICT_DEVMEM).', context=context_data)
                logger.error('failed_to_write_address_hex_error_msg', address_hex=cfg.address_hex, error_msg=error_msg)
                return ValidatorResult(passed=False, error_msg=f'MMIO Write Failed: {error_msg}', context=context_data)
            logger.info('successfully_injected_write_val_hex_into_address_hex', write_val_hex=write_val_hex, address_hex=cfg.address_hex)
            return ValidatorResult(passed=True, metrics={'t_mmio_write_s': res.duration_s}, context=context_data)
        except TransportTimeoutError:
            logger.critical('fatal_axi_ahb_bus_hang_silicon_locked_up_during_write_to_address_hex', address_hex=cfg.address_hex)
            return ValidatorResult(passed=False, error_msg='Bus Hang: Silicon locked up during write.', context=context_data)
        except TransportConnectionError as e:
            logger.critical('fatal_kernel_panic_data_abort_triggered_by_mmio_write_to_address_hex_e', address_hex=cfg.address_hex, e=e)
            return ValidatorResult(passed=False, error_msg=f'Kernel Panic/Data Abort during MMIO write: {e}', context=context_data)
