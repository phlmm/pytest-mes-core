# src/pytest_mes_core/protocols/memory.py
import time
import secrets
import logging
import base64

from tenacity import retry, stop_after_attempt, wait_fixed

from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult
from pytest_mes_core.config import MtdFlashConfig, I2cEepromConfig

logger = logging.getLogger("mes_core.protocols.memory")

class MemoryValidator:
    """Validates I2C EEPROM integrity, respecting silicon page-write boundaries."""

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(0.5), reraise=True)
    def verify_i2c_read_write(dut_ssh: EphemeralSSHClient, cfg: I2cEepromConfig) -> ValidatorResult:
        # Defensive check: Prevent silicon page wrap-around corruption
        reg_int = int(cfg.test_register, 16)
        if (reg_int % cfg.page_size_bytes) + cfg.num_bytes > cfg.page_size_bytes:
            logger.error("[I2C] Illegal write! Payload crosses physical EEPROM page boundary.")
            raise ValueError(f"FATAL: I2C write crosses page boundary of {cfg.page_size_bytes} bytes.")

        random_bytes = [secrets.randbelow(256) for _ in range(cfg.num_bytes)]
        hex_payload = " ".join([f"0x{b:02X}" for b in random_bytes])

        logger.info(f"[I2C Bus {cfg.bus}] Writing {cfg.num_bytes} bytes to Device {cfg.address} Reg {cfg.test_register}...")

        # 1. Write to Silicon
        cmd_write = f"i2ctransfer -y {cfg.bus} w{cfg.num_bytes}@{cfg.address} {cfg.test_register} {hex_payload}"
        res_write = dut_ssh.safe_run(cmd_write, timeout_s=5.0)

        if not res_write.ok:
            logger.error(f"[I2C] Write failed. Bus locked? Err: {res_write.stderr.strip()}")
            return ValidatorResult(passed=False, error_msg="I2C Write command rejected by kernel.")

        # 2. Acknowledge Hardware Physics
        logger.debug(f"[I2C] Awaiting {cfg.write_delay_s}s for silicon page-write cycle (tW)...")
        time.sleep(cfg.write_delay_s)

        # 3. Read from Silicon
        cmd_read = f"i2ctransfer -y {cfg.bus} w1@{cfg.address} {cfg.test_register} r{cfg.num_bytes}"
        res_read = dut_ssh.safe_run(cmd_read, timeout_s=5.0)

        if not res_read.ok:
            return ValidatorResult(passed=False, error_msg="I2C Read command rejected by kernel.")

        try:
            read_hex_strings = res_read.stdout.strip().split()
            read_bytes = [int(h, 16) for h in read_hex_strings if h.startswith("0x")]
        except ValueError as e:
            logger.error(f"[I2C] Failed to parse readback: {res_read.stdout.strip()}")
            return ValidatorResult(passed=False, error_msg=f"Corrupt readback data: {e}")

        passed = (read_bytes == random_bytes)
        if not passed:
            logger.critical(f"[I2C] VERIFICATION FAILED! Wrote: {random_bytes}, Read: {read_bytes}")
            return ValidatorResult(passed=False, error_msg="I2C Readback mismatch (Silicon corrupted).")

        logger.info("[I2C] Readback matches cryptographic payload exactly.")
        return ValidatorResult(passed=True, context={"i2c_bus": cfg.bus, "i2c_address": cfg.address})


class MtdFlashValidator:
    """Validates SPI NOR/NAND Flash via the Linux MTD subsystem."""

    @staticmethod
    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1.0), reraise=True)
    def verify_scratch_sector(dut_ssh: EphemeralSSHClient, cfg: MtdFlashConfig) -> ValidatorResult:
        logger.warning(f"[MTD] Destructive write on {cfg.mtd_dev} at {cfg.sector_offset_hex}")

        try:
            # 1. Erase
            erase_cmd = f"flash_erase {cfg.mtd_dev} {cfg.sector_offset_hex} {cfg.erase_blocks}"
            logger.debug(f"[MTD] Executing: {erase_cmd}")
            res_erase = dut_ssh.safe_run(erase_cmd, timeout_s=10.0)

            if not res_erase.ok:
                logger.error(f"[MTD] Erase failed: {res_erase.stderr.strip()}")
                return ValidatorResult(passed=False, error_msg="flash_erase failed.")

            time.sleep(0.1) # Charge pump settling

            # Calculate blocks
            if cfg.test_bytes % cfg.page_size_bytes != 0:
                raise ValueError("FATAL: test_bytes must be a multiple of page_size_bytes.")

            block_count = cfg.test_bytes // cfg.page_size_bytes
            offset_dec = int(cfg.sector_offset_hex, 16) // cfg.page_size_bytes

            # 2. Write
            payload = secrets.token_bytes(cfg.test_bytes)
            b64_payload = base64.b64encode(payload).decode('utf-8')

            write_cmd = (
                f"echo '{b64_payload}' | base64 -d | "
                f"dd of={cfg.mtd_dev} bs={cfg.page_size_bytes} seek={offset_dec} count={block_count} conv=notrunc"
            )

            logger.debug(f"[MTD] Transmitting {cfg.test_bytes} bytes via base64 dd pipeline...")
            t0 = time.perf_counter()
            res_write = dut_ssh.safe_run(write_cmd, timeout_s=15.0)
            write_duration = round(time.perf_counter() - t0, 3)

            if not res_write.ok:
                logger.error(f"[MTD] Write failed: {res_write.stderr.strip()}")
                return ValidatorResult(passed=False, error_msg="MTD write rejected.")

            # 3. Readback and Verify
            abs_offset = int(cfg.sector_offset_hex, 16)
            read_cmd = f"hexdump -v -e '1/1 \"%02X\"' -s {abs_offset} -n {cfg.test_bytes} {cfg.mtd_dev}"
            res_read = dut_ssh.safe_run(read_cmd, timeout_s=5.0)
            actual_hex = res_read.stdout.strip()

            if payload.hex().upper() != actual_hex:
                logger.critical(f"[MTD] CORRUPTION DETECTED! Wrote: {payload.hex().upper()}, Read: {actual_hex}")
                return ValidatorResult(passed=False, error_msg="MTD Payload mismatch.")

            logger.info(f"[MTD] Verified {cfg.test_bytes} bytes successfully in {write_duration}s.")
            return ValidatorResult(passed=True, metrics={"mtd_write_time_s": write_duration})

        finally:
            # 4. ZERO-LEAKAGE: Erase the scratch sector again so we don't leave garbage on the flash
            logger.debug(f"[MTD] ZERO-LEAKAGE: Wiping scratch sector {cfg.sector_offset_hex} on {cfg.mtd_dev}.")
            dut_ssh.safe_run(f"flash_erase {cfg.mtd_dev} {cfg.sector_offset_hex} {cfg.erase_blocks}")
