# src/pytest_mes_core/protocols/mmio.py
import logging
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import MmioConfig

logger = logging.getLogger("mes_core.protocols.mmio")

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
        logger.info(f"[MMIO] Reading {cfg.data_width}-bit hardware register at {cfg.address_hex}...")

        context_data: Dict[str, Any] = {"address": cfg.address_hex, "mask": cfg.bit_mask_hex}

        # We use a strict 3-second timeout. If the bus hangs (unclocked domain),
        # devmem won't return, and we need to catch it quickly.
        cmd = f"devmem {cfg.address_hex} {cfg.data_width}"
        logger.debug(f"[MMIO] Executing kernel bypass: {cmd}")

        try:
            res = dut.safe_run(cmd, timeout_s=3.0)

            if not res.ok:
                error_msg = res.stderr.strip() or res.stdout.strip()

                #  FORENSIC KERNEL CONFIG INTERCEPTOR
                if "Operation not permitted" in error_msg:
                    logger.critical("="*60)
                    logger.critical("[MMIO] FATAL: KERNEL BLOCKED PHYSICAL MEMORY ACCESS!")
                    logger.critical("[MMIO] CONFIG_STRICT_DEVMEM is enabled in the Linux kernel.")
                    logger.critical("[MMIO] You must disable it in the kernel defconfig to use this validator.")
                    logger.critical("="*60)
                    return ValidatorResult(passed=False, error_msg="Kernel blocked access (CONFIG_STRICT_DEVMEM enabled).", context=context_data)

                logger.error(f"[MMIO] Failed to read {cfg.address_hex}: {error_msg}")
                return ValidatorResult(passed=False, error_msg=f"MMIO Read Failed: {error_msg}", context=context_data)

            # Parse the raw hex output
            raw_val = int(res.stdout.strip(), 16)
            mask = int(cfg.bit_mask_hex, 16)
            masked_val = raw_val & mask

            hex_result = hex(masked_val).upper()
            context_data["mmio_raw_val"] = hex(raw_val).upper()
            context_data["mmio_masked_val"] = hex_result

            logger.debug(f"[MMIO] Raw: {context_data['mmio_raw_val']} | Mask: {cfg.bit_mask_hex} -> Result: {hex_result}")

            # Optional Verification
            if cfg.expected_value_hex:
                expected = int(cfg.expected_value_hex, 16)
                if masked_val != expected:
                    logger.warning(f"[MMIO] MISMATCH: Expected {hex(expected).upper()}, got {hex_result}")
                    return ValidatorResult(
                        passed=False,
                        error_msg=f"MMIO Mismatch (Expected {hex(expected).upper()}, Got {hex_result})",
                        metrics={"t_mmio_read_s": res.duration_s},
                        context=context_data
                    )

            logger.info(f"[MMIO] Register evaluation passed: {hex_result}")
            return ValidatorResult(
                passed=True,
                metrics={"t_mmio_read_s": res.duration_s},
                context=context_data
            )

        except ValueError as e:
            logger.error(f"[MMIO] Invalid devmem output: {res.stdout.strip()}")
            return ValidatorResult(passed=False, error_msg=f"Failed to parse devmem output: {e}", context=context_data)

        except TransportTimeoutError:
            # The AXI/AHB bus locked up waiting for an unclocked peripheral to respond
            logger.critical("="*60)
            logger.critical(f"[MMIO] FATAL: AXI/AHB BUS HANG DETECTED!")
            logger.critical(f"[MMIO] Silicon locked up reading {cfg.address_hex}.")
            logger.critical("[MMIO] Target peripheral clock is likely disabled (unclocked domain).")
            logger.critical("="*60)
            return ValidatorResult(passed=False, error_msg="Bus Hang: Silicon locked up (Unclocked domain?).", context=context_data)

        except TransportConnectionError as e:
            # The read triggered an asynchronous Data Abort, instantly crashing the kernel
            logger.critical("="*60)
            logger.critical(f"[MMIO] FATAL: KERNEL PANIC / DATA ABORT DETECTED!")
            logger.critical(f"[MMIO] Hardware aggressively rejected access to {cfg.address_hex}: {e}")
            logger.critical("[MMIO] You are likely reading unmapped or protected TrustZone memory.")
            logger.critical("="*60)
            return ValidatorResult(passed=False, error_msg=f"Kernel Panic/Data Abort accessing MMIO: {e}", context=context_data)


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
        context_data: Dict[str, Any] = {"address": cfg.address_hex, "write_val": write_val_hex}

        logger.warning("="*60)
        logger.warning(f"[MMIO]  DANGER: FORCING HARDWARE WRITE TO {cfg.address_hex} ")
        logger.warning(f"[MMIO] Injecting Value: {write_val_hex} ({cfg.data_width}-bit)")
        logger.warning("="*60)

        cmd = f"devmem {cfg.address_hex} {cfg.data_width} {write_val_hex}"
        logger.debug(f"[MMIO] Executing kernel bypass: {cmd}")

        try:
            res = dut.safe_run(cmd, timeout_s=3.0)

            if not res.ok:
                error_msg = res.stderr.strip() or res.stdout.strip()

                # Check for strict kernel memory protection on write
                if "Operation not permitted" in error_msg:
                    logger.critical(f"[MMIO] FATAL: Kernel blocked access. CONFIG_STRICT_DEVMEM is enabled.")
                    return ValidatorResult(passed=False, error_msg="Kernel blocked write (CONFIG_STRICT_DEVMEM).", context=context_data)

                logger.error(f"[MMIO] Failed to write {cfg.address_hex}: {error_msg}")
                return ValidatorResult(passed=False, error_msg=f"MMIO Write Failed: {error_msg}", context=context_data)

            logger.info(f"[MMIO] Successfully injected {write_val_hex} into {cfg.address_hex}.")
            return ValidatorResult(
                passed=True,
                metrics={"t_mmio_write_s": res.duration_s},
                context=context_data
            )

        except TransportTimeoutError:
            logger.critical(f"[MMIO] FATAL: AXI/AHB Bus Hang. Silicon locked up during write to {cfg.address_hex}.")
            return ValidatorResult(passed=False, error_msg="Bus Hang: Silicon locked up during write.", context=context_data)
        except TransportConnectionError as e:
            logger.critical(f"[MMIO] FATAL: Kernel Panic / Data Abort triggered by MMIO write to {cfg.address_hex}: {e}")
            return ValidatorResult(passed=False, error_msg=f"Kernel Panic/Data Abort during MMIO write: {e}", context=context_data)
