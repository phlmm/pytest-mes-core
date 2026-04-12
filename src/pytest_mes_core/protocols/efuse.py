import time
import base64
import logging
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.protocols.efuse")

class NvmemEfuseValidator:
    @staticmethod
    def read_efuse(dut_ssh: EphemeralSSHClient, nvmem_path: str, offset_hex: str, num_bytes: int) -> str:
        logger.debug(f"[eFuse] Reading {num_bytes} bytes from {nvmem_path} at {offset_hex}...")
        cmd = f"hexdump -v -e '/1 \"%02X\"' -s {offset_hex} -n {num_bytes} {nvmem_path}"
        res = dut_ssh.conn.run(cmd, hide=True, warn=True)

        if not res.ok:
            logger.error(f"[eFuse] NVMEM read failed: {res.stderr.strip()}")
            raise RuntimeError(f"FATAL: Failed to read nvmem at {nvmem_path}.")

        val = res.stdout.strip()
        logger.debug(f"[eFuse] Silicon returned: {val}")
        return val

    @staticmethod
    def burn_efuse(
        dut_ssh: EphemeralSSHClient, nvmem_path: str, offset_hex: str, hex_payload: str
    ) -> ValidatorResult:
        logger.warning(f"[eFuse] INITIATING PERMANENT SILICON BURN at {offset_hex}. Payload: {hex_payload}")

        payload_bytes = bytes.fromhex(hex_payload)
        num_bytes = len(payload_bytes)

        current_val = NvmemEfuseValidator.read_efuse(dut_ssh, nvmem_path, offset_hex, num_bytes)
        if current_val == hex_payload:
            logger.info(f"[eFuse] Target region {offset_hex} already holds target payload. Skipping.")
            return ValidatorResult(passed=True, context={"status": "already_burned"})

        if current_val != "00" * num_bytes and current_val != "FF" * num_bytes:
            logger.error(f"[eFuse] Region dirty! Refusing to logically OR corrupt eFuses. Reads: {current_val}")
            return ValidatorResult(passed=False, error_msg=f"Region {offset_hex} is dirty.")

        b64_payload = base64.b64encode(payload_bytes).decode('utf-8')
        offset_dec = int(offset_hex, 16)

        burn_cmd = f"echo '{b64_payload}' | base64 -d | dd of={nvmem_path} bs=1 seek={offset_dec} count={num_bytes} conv=notrunc"
        logger.debug(f"[eFuse] Transmitting base64 dd pipeline: {burn_cmd}")

        t0 = time.perf_counter()
        burn_res = dut_ssh.conn.run(burn_cmd, hide=True, warn=True)
        duration = round(time.perf_counter() - t0, 3)
        time.sleep(0.5) # Charge pump settling

        if not burn_res.ok:
            logger.error(f"[eFuse] Kernel rejected write. Output: {burn_res.stderr.strip()}")
            return ValidatorResult(passed=False, error_msg="Kernel rejected eFuse burn.")

        readback_val = NvmemEfuseValidator.read_efuse(dut_ssh, nvmem_path, offset_hex, num_bytes)
        if readback_val != hex_payload.upper():
            logger.critical(f"[eFuse] VERIFICATION FAILED! Wrote {hex_payload}, Read {readback_val}")
            return ValidatorResult(passed=False, error_msg="Burn verification failed.")

        logger.info(f"[eFuse] Verification passed. {num_bytes} bytes burned in {duration}s.")
        return ValidatorResult(passed=True, metrics={"t_efuse_burn_s": duration}, context={"payload": hex_payload})
