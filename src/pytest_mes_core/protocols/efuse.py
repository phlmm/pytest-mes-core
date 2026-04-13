import time
import base64
import logging
from typing import Dict, Any

from pytest_mes_core.transports import DutTransport
from pytest_mes_core.protocols import ValidatorResult
from pytest_mes_core.config import EfuseConfig

logger = logging.getLogger("mes_core.protocols.efuse")

class NvmemEfuseValidator:

    @classmethod
    def read_efuse(cls, dut: DutTransport, cfg: EfuseConfig, offset_hex: str, num_bytes: int) -> str:
        cmd = f"hexdump -v -e '/1 \"%02X\"' -s {offset_hex} -n {num_bytes} {cfg.nvmem_path}"
        res = dut.safe_run(cmd, timeout_s=5.0)

        if not res.ok:
            raise IOError(f"NVMEM read failed: {res.stderr.strip()}")

        val = res.stdout.strip().upper()
        if len(val) != num_bytes * 2:
            raise IOError(f"Hexdump returned {len(val)//2} bytes, expected {num_bytes}. (Read: {val})")

        return val

    @classmethod
    def burn_efuse(
        cls,
        dut: DutTransport,
        cfg: EfuseConfig,
        offset_hex: str,
        hex_payload: str
    ) -> ValidatorResult:

        hex_payload = hex_payload.replace(" ", "").replace("0x", "").upper()
        payload_bytes = bytes.fromhex(hex_payload)
        num_bytes = len(payload_bytes)
        offset_dec = int(offset_hex, 16)
        context_data: Dict[str, Any] = {"target_offset": offset_hex, "payload": hex_payload}

        logger.warning(f"[eFuse] 🚨 INITIATING PERMANENT SILICON BURN at {offset_hex}. Payload: {hex_payload} 🚨")

        # 1. Dynamic Alignment Check based on TOML
        if cfg.require_32bit_alignment and (offset_dec % 4 != 0 or num_bytes % 4 != 0):
            logger.warning(
                f"[eFuse] UNALIGNED ACCESS: Offset {offset_hex} or Size {num_bytes} "
                f"violates the 32-bit alignment constraint specified in the TOML!"
            )
            context_data["alignment_warning"] = True

        # 2. Pre-Burn State Validation
        try:
            current_val = cls.read_efuse(dut, cfg, offset_hex, num_bytes)
            context_data["pre_burn_state"] = current_val
        except IOError as e:
            return ValidatorResult(passed=False, error_msg=str(e), context=context_data)

        if current_val == hex_payload:
            logger.info(f"[eFuse] Target region {offset_hex} already holds payload. Skipping.")
            return ValidatorResult(passed=True, context={"status": "already_burned", **context_data})

        if current_val != "00" * num_bytes:
            return ValidatorResult(passed=False, error_msg=f"Region {offset_hex} is dirty.", context=context_data)

        # 3. The Physical Burn
        b64_payload = base64.b64encode(payload_bytes).decode('utf-8')
        burn_cmd = f"echo '{b64_payload}' | base64 -d | dd of={cfg.nvmem_path} bs=1 seek={offset_dec} count={num_bytes} conv=notrunc"

        t0 = time.perf_counter()
        burn_res = dut.safe_run(burn_cmd, timeout_s=10.0)
        duration = round(time.perf_counter() - t0, 3)
        time.sleep(0.5)

        # 4. Configurable Forensic Intercept
        if not burn_res.ok:
            logger.error(f"[eFuse] Kernel rejected write. Scraping dmesg with pattern: '{cfg.dmesg_grep_pattern}'...")

            # Use the TOML-injected regex pattern!
            dmesg_cmd = f"dmesg | grep -iE '{cfg.dmesg_grep_pattern}' | tail -n 10"
            dmesg_res = dut.safe_run(dmesg_cmd, timeout_s=5.0)

            if dmesg_res.ok and dmesg_res.stdout:
                context_data["kernel_efuse_trace"] = dmesg_res.stdout.strip()
                logger.debug(f"[eFuse] Kernel Trace: \n{context_data['kernel_efuse_trace']}")
            else:
                context_data["stderr"] = burn_res.stderr.strip()

            return ValidatorResult(passed=False, error_msg="Kernel rejected eFuse burn.", context=context_data)

        # 5. Readback Verification
        try:
            readback_val = cls.read_efuse(dut, cfg, offset_hex, num_bytes)
            context_data["post_burn_state"] = readback_val
        except IOError as e:
            return ValidatorResult(passed=False, error_msg=f"Post-burn read failed: {e}", context=context_data)

        if readback_val != hex_payload:
            return ValidatorResult(
                passed=False,
                error_msg=f"Burn verification failed. Read: {readback_val}",
                context=context_data
            )

        return ValidatorResult(passed=True, metrics={"t_efuse_burn_s": duration}, context=context_data)
