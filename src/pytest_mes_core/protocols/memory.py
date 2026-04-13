import time
import secrets
import logging
import base64
from typing import Dict, Any

from pytest_mes_core.transports import (
    DutTransport,
    TransportConnectionError,
    TransportTimeoutError
)
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import MtdFlashConfig, I2cEepromConfig

logger = logging.getLogger("mes_core.protocols.memory")

class MemoryValidator:
    """Validates I2C EEPROM integrity, respecting silicon page-write boundaries."""

    @staticmethod
    def verify_i2c_read_write(dut: DutTransport, cfg: I2cEepromConfig) -> ValidatorResult:
        # 1. Defensive check: Prevent silicon page wrap-around corruption
        reg_int = int(cfg.test_register, 16)
        if (reg_int % cfg.page_size_bytes) + cfg.num_bytes > cfg.page_size_bytes:
            logger.error("[I2C] Illegal write! Payload crosses physical EEPROM page boundary.")
            return ValidatorResult(
                passed=False,
                error_msg=f"Test Configuration Error: Write crosses EEPROM page boundary of {cfg.page_size_bytes} bytes."
            )

        random_bytes = [secrets.randbelow(256) for _ in range(cfg.num_bytes)]
        hex_payload = " ".join([f"0x{b:02X}" for b in random_bytes])
        context_data: Dict[str, Any] = {"i2c_bus": cfg.bus, "i2c_address": cfg.address, "test_register": cfg.test_register}

        logger.info(f"[I2C Bus {cfg.bus}] Writing {cfg.num_bytes} bytes to Device {cfg.address} Reg {cfg.test_register}...")

        try:
            # 2. Write to Silicon
            cmd_write = f"i2ctransfer -y {cfg.bus} w{cfg.num_bytes}@{cfg.address} {cfg.test_register} {hex_payload}"
            res_write = dut.safe_run(cmd_write, timeout_s=5.0)

            if not res_write.ok:
                logger.error(f"[I2C] Write failed. Bus locked? Err: {res_write.stderr.strip()}")
                return ValidatorResult(passed=False, error_msg=f"I2C Write rejected: {res_write.stderr.strip()}", context=context_data)

            # 3. Acknowledge Hardware Physics (tW)
            logger.debug(f"[I2C] Awaiting {cfg.write_delay_s}s for silicon page-write cycle (tW)...")
            time.sleep(cfg.write_delay_s)

            # 4. Read from Silicon
            cmd_read = f"i2ctransfer -y {cfg.bus} w1@{cfg.address} {cfg.test_register} r{cfg.num_bytes}"
            res_read = dut.safe_run(cmd_read, timeout_s=5.0)

            if not res_read.ok:
                return ValidatorResult(passed=False, error_msg=f"I2C Read rejected: {res_read.stderr.strip()}", context=context_data)

            try:
                read_hex_strings = res_read.stdout.strip().split()
                # Ensure we only parse actual hex bytes to prevent ValueError on kernel warnings
                read_bytes = [int(h, 16) for h in read_hex_strings if h.startswith("0x")]

                if len(read_bytes) != cfg.num_bytes:
                    raise ValueError(f"Expected {cfg.num_bytes} bytes, got {len(read_bytes)}.")

            except ValueError as e:
                logger.error(f"[I2C] Failed to parse readback: {res_read.stdout.strip()}")
                return ValidatorResult(passed=False, error_msg=f"Corrupt readback data: {e}", context=context_data)

            # 5. Logical Evaluation
            passed = (read_bytes == random_bytes)
            if not passed:
                logger.critical(f"[I2C] VERIFICATION FAILED! Wrote: {random_bytes}, Read: {read_bytes}")
                return ValidatorResult(passed=False, error_msg="I2C Readback mismatch (Silicon corrupted).", context=context_data)

            logger.info("[I2C] Readback matches cryptographic payload exactly.")
            return ValidatorResult(
                passed=True,
                metrics={"t_i2c_write_s": res_write.duration_s, "t_i2c_read_s": res_read.duration_s},
                context=context_data
            )

        except TransportTimeoutError:
            return ValidatorResult(passed=False, error_msg="DUT hung during I2C transaction. Bus locked?", context=context_data)
        except TransportConnectionError as e:
            return ValidatorResult(passed=False, error_msg=f"Transport dropped during I2C transaction: {e}", context=context_data)


class MtdFlashValidator:
    """Validates SPI NOR/NAND Flash via the Linux MTD subsystem."""

    @staticmethod
    def verify_scratch_sector(dut: DutTransport, cfg: MtdFlashConfig) -> ValidatorResult:
        logger.warning(f"[MTD] Destructive write on {cfg.mtd_dev} at {cfg.sector_offset_hex}")
        context_data: Dict[str, Any] = {"mtd_device": cfg.mtd_dev, "offset": cfg.sector_offset_hex}

        # Calculate blocks pre-flight
        if cfg.test_bytes % cfg.page_size_bytes != 0:
            return ValidatorResult(passed=False, error_msg="Test Configuration Error: test_bytes must be a multiple of page_size_bytes.")

        block_count = cfg.test_bytes // cfg.page_size_bytes
        offset_dec = int(cfg.sector_offset_hex, 16) // cfg.page_size_bytes
        payload = secrets.token_bytes(cfg.test_bytes)
        b64_payload = base64.b64encode(payload).decode('utf-8')

        try:
            # ==========================================
            # 1. PHYSICAL ERASE
            # ==========================================
            erase_cmd = f"flash_erase {cfg.mtd_dev} {cfg.sector_offset_hex} {cfg.erase_blocks}"
            res_erase = dut.safe_run(erase_cmd, timeout_s=10.0)

            if not res_erase.ok:
                return ValidatorResult(passed=False, error_msg=f"flash_erase failed: {res_erase.stderr.strip()}", context=context_data)

            time.sleep(0.1) # Charge pump settling

            # ==========================================
            # 2. PHYSICAL WRITE
            # ==========================================
            write_cmd = (
                f"echo '{b64_payload}' | base64 -d | "
                # We add fsync here to guarantee the kernel commits the write to NAND before returning!
                f"dd of={cfg.mtd_dev} bs={cfg.page_size_bytes} seek={offset_dec} count={block_count} conv=notrunc,fsync"
            )

            logger.debug(f"[MTD] Transmitting {cfg.test_bytes} bytes via base64 dd pipeline...")
            res_write = dut.safe_run(write_cmd, timeout_s=15.0)

            if not res_write.ok:
                logger.error(f"[MTD] Write failed: {res_write.stderr.strip()}")
                return ValidatorResult(passed=False, error_msg=f"MTD write rejected: {res_write.stderr.strip()}", context=context_data)

            # ==========================================
            # 3. PHYSICAL READBACK & VERIFY
            # ==========================================
            abs_offset = int(cfg.sector_offset_hex, 16)
            read_cmd = f"hexdump -v -e '1/1 \"%02X\"' -s {abs_offset} -n {cfg.test_bytes} {cfg.mtd_dev}"
            res_read = dut.safe_run(read_cmd, timeout_s=10.0)

            if not res_read.ok:
                 return ValidatorResult(passed=False, error_msg=f"MTD readback failed: {res_read.stderr.strip()}", context=context_data)

            actual_hex = res_read.stdout.strip()

            if payload.hex().upper() != actual_hex:
                logger.critical(f"[MTD] CORRUPTION DETECTED! Wrote: {payload.hex().upper()}, Read: {actual_hex}")
                return ValidatorResult(passed=False, error_msg="MTD Payload mismatch (Flash wearout / bad block).", context=context_data)

            logger.info(f"[MTD] Verified {cfg.test_bytes} bytes successfully in {res_write.duration_s}s.")
            return ValidatorResult(
                passed=True,
                metrics={"t_mtd_write_s": res_write.duration_s, "t_mtd_read_s": res_read.duration_s},
                context=context_data
            )

        except TransportTimeoutError:
            return ValidatorResult(passed=False, error_msg="Silicon Lockup: DUT hung during MTD flash operation.", context=context_data)
        except TransportConnectionError as e:
            return ValidatorResult(passed=False, error_msg=f"Transport pipe shattered (Brownout during flash erase/write?): {e}", context=context_data)

        finally:
            # ==========================================
            # 4. ZERO-LEAKAGE TEARDOWN
            # ==========================================
            if dut.is_connected:
                try:
                    logger.debug(f"[MTD] ZERO-LEAKAGE: Wiping scratch sector {cfg.sector_offset_hex} on {cfg.mtd_dev}.")
                    dut.safe_run(f"flash_erase {cfg.mtd_dev} {cfg.sector_offset_hex} {cfg.erase_blocks}", timeout_s=10.0)
                except Exception as cleanup_err:
                    logger.debug(f"[MTD] Final wipe skipped (Transport likely dead): {cleanup_err}")
