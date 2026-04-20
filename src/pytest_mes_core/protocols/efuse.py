# src/pytest_mes_core/protocols/efuse.py
import time
import base64
import logging
from typing import Dict, Any

from pytest_mes_core.transports import DutTransport
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import EfuseConfig

logger = logging.getLogger("mes_core.protocols.efuse")

class NvmemEfuseValidator:
    """Interacts with the SoC's hardware eFuse blocks via the Linux NVMEM subsystem.

    Provides high-assurance read and permanent burn capabilities, complete with
    pre-burn state validation, strict alignment checks, and post-burn readback verification.
    """

    @classmethod
    def read_efuse(cls, dut: DutTransport, cfg: EfuseConfig, offset_hex: str, num_bytes: int) -> str:
        """Reads a specific number of bytes from the eFuse block.

        Args:
            dut: The transport interface connected to the Device Under Test.
            cfg: The eFuse configuration parameters including the NVMEM path.
            offset_hex: The hexadecimal offset within the NVMEM device to read from.
            num_bytes: The number of bytes to read.

        Returns:
            str: A hex string representation of the bytes read from the eFuse.

        Raises:
            IOError: If the NVMEM read command fails or returns an unexpected number of bytes.
        """
        logger.debug(f"[eFuse] Reading {num_bytes} bytes from NVMem offset {offset_hex}...")
        cmd = f"hexdump -v -e '/1 \"%02X\"' -s {offset_hex} -n {num_bytes} {cfg.nvmem_path}"
        res = dut.safe_run(cmd, timeout_s=5.0)

        if not res.ok:
            err_msg = f"NVMEM read failed: {res.stderr.strip()}"
            logger.error(f"[eFuse] {err_msg}")
            raise IOError(err_msg)

        val = res.stdout.strip().upper()
        if len(val) != num_bytes * 2:
            err_msg = f"Hexdump returned {len(val)//2} bytes, expected {num_bytes}. (Read: {val})"
            logger.error(f"[eFuse] {err_msg}")
            raise IOError(err_msg)

        logger.debug(f"[eFuse] Read successful: 0x{val}")
        return val

    @classmethod
    def burn_efuse(
        cls,
        dut: DutTransport,
        cfg: EfuseConfig,
        offset_hex: str,
        hex_payload: str
    ) -> ValidatorResult:
        """Permanently burns a hex payload into the SoC's eFuse memory.

        Executes a sequence of safety checks: validates 32-bit alignment if required,
        verifies the target region is blank (all 00s), executes the burn via base64
        injection to `dd`, and finally performs a readback to guarantee the silicon
        accepted the charge.

        Args:
            dut: The transport interface connected to the Device Under Test.
            cfg: The eFuse configuration parameters.
            offset_hex: The hexadecimal offset to burn the payload to.
            hex_payload: The data to burn, formatted as a hex string.

        Returns:
            ValidatorResult: An object containing the validation outcome (passed/failed), 
                captured metrics like burn duration, and contextual error/trace information.
        """

        hex_payload = hex_payload.replace(" ", "").replace("0x", "").upper()
        payload_bytes = bytes.fromhex(hex_payload)
        num_bytes = len(payload_bytes)
        offset_dec = int(offset_hex, 16)
        context_data: Dict[str, Any] = {"target_offset": offset_hex, "payload": hex_payload}

        logger.warning("="*60)
        logger.warning(f"[eFuse]  DANGER: INITIATING PERMANENT SILICON BURN at {offset_hex} ")
        logger.warning(f"[eFuse] Payload: {hex_payload} ({num_bytes} bytes)")
        logger.warning("="*60)

        # 1. Dynamic Alignment Check based on TOML
        if cfg.require_32bit_alignment and (offset_dec % 4 != 0 or num_bytes % 4 != 0):
            err_msg = (
                f"UNALIGNED ACCESS: Offset {offset_hex} or Size {num_bytes} "
                f"violates the 32-bit alignment constraint specified in the TOML!"
            )
            logger.critical(f"[eFuse] FATAL: {err_msg}")
            context_data["alignment_warning"] = True

        # 2. Pre-Burn State Validation
        try:
            current_val = cls.read_efuse(dut, cfg, offset_hex, num_bytes)
            context_data["pre_burn_state"] = current_val
        except IOError as e:
            return ValidatorResult(passed=False, error_msg=str(e), context=context_data)

        if current_val == hex_payload:
            logger.info(f"[eFuse] Target region {offset_hex} already holds exact payload. Skipping burn.")
            return ValidatorResult(passed=True, context={"status": "already_burned", **context_data})

        if current_val != "00" * num_bytes:
            err_msg = f"Region {offset_hex} is dirty (Contains: {current_val}). Cannot overwrite one-time programmable memory!"
            logger.critical(f"[eFuse] FATAL: {err_msg} (Board is permanently dead/bricked)")
            return ValidatorResult(passed=False, error_msg=err_msg, context=context_data)

        # 3. The Physical Burn
        logger.debug(f"[eFuse] Formatting {num_bytes}-byte payload for Base64 POSIX injection...")
        b64_payload = base64.b64encode(payload_bytes).decode('utf-8')
        burn_cmd = f"echo '{b64_payload}' | base64 -d | dd of={cfg.nvmem_path} bs=1 seek={offset_dec} count={num_bytes} conv=notrunc"

        logger.debug("[eFuse] Executing kernel NVMem write...")
        t0 = time.perf_counter()
        burn_res = dut.safe_run(burn_cmd, timeout_s=10.0)
        duration = round(time.perf_counter() - t0, 3)

        logger.debug("[eFuse] Write complete. Delaying 0.5s for silicon charge pumps to settle...")
        time.sleep(0.5)

        # 4. Configurable Forensic Intercept
        if not burn_res.ok:
            logger.error("[eFuse] Kernel rejected write! Scraping dmesg for NVMEM/eFuse faults...")

            # Use the TOML-injected regex pattern!
            dmesg_cmd = f"dmesg | grep -iE '{cfg.dmesg_grep_pattern}' | tail -n 10"
            dmesg_res = dut.safe_run(dmesg_cmd, timeout_s=5.0)

            if dmesg_res.ok and dmesg_res.stdout:
                context_data["kernel_efuse_trace"] = dmesg_res.stdout.strip()
                logger.critical("="*60)
                logger.critical(f"[eFuse] FATAL: Kernel NVMem subsystem fault detected:\n{context_data['kernel_efuse_trace']}")
                logger.critical("="*60)
            else:
                context_data["stderr"] = burn_res.stderr.strip()
                logger.critical(f"[eFuse] FATAL: Command failed without kernel trace. Stderr: {context_data['stderr']}")

            return ValidatorResult(passed=False, error_msg="Kernel rejected eFuse burn.", context=context_data)

        # 5. Readback Verification
        logger.debug("[eFuse] Commencing hardware readback verification...")
        try:
            readback_val = cls.read_efuse(dut, cfg, offset_hex, num_bytes)
            context_data["post_burn_state"] = readback_val
        except IOError as e:
            logger.critical(f"[eFuse] FATAL: Post-burn readback failed entirely! Silicon locked? {e}")
            return ValidatorResult(passed=False, error_msg=f"Post-burn read failed: {e}", context=context_data)

        if readback_val != hex_payload:
            err_msg = f"Burn verification failed. Expected: {hex_payload}, Read: {readback_val}"
            logger.critical(f"[eFuse] FATAL: {err_msg} (Hardware defect in SoC eFuse controller!)")
            return ValidatorResult(
                passed=False,
                error_msg=err_msg,
                context=context_data
            )

        logger.info(f"[eFuse] Successfully burned and verified {num_bytes} bytes at {offset_hex} in {duration}s.")
        return ValidatorResult(passed=True, metrics={"t_efuse_burn_s": duration}, context=context_data)
