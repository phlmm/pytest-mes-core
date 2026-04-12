import logging
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult
from pytest_mes_core.config import MmioConfig

logger = logging.getLogger("mes_core.protocols.mmio")

class MmioValidator:
    """Reads raw physical silicon addresses bypassing Linux kernel drivers."""

    @staticmethod
    def read_register(dut_ssh: EphemeralSSHClient, cfg: MmioConfig) -> ValidatorResult:
        logger.debug(f"[MMIO] Reading {cfg.data_width}-bit register at {cfg.address_hex}...")

        # We use a strict 3-second timeout. If the bus hangs (unclocked domain),
        # devmem won't return, and we need to catch it quickly.
        cmd = f"devmem {cfg.address_hex} {cfg.data_width}"
        res = dut_ssh.safe_run(cmd, timeout_s=3.0)

        if not res.ok:
            logger.error(f"[MMIO] Failed to read {cfg.address_hex}. Unclocked domain? {res.stderr}")
            return ValidatorResult(passed=False, error_msg="MMIO Read Failed (Bus Hang / Permission Denied)")

        try:
            # Parse the raw hex output
            raw_val = int(res.stdout.strip(), 16)
            mask = int(cfg.bit_mask_hex, 16)
            masked_val = raw_val & mask

            hex_result = hex(masked_val).upper()

            logger.info(f"[MMIO] {cfg.address_hex} & {cfg.bit_mask_hex} = {hex_result}")

            # Optional Verification
            if cfg.expected_value_hex:
                expected = int(cfg.expected_value_hex, 16)
                if masked_val != expected:
                    logger.warning(f"[MMIO] MISMATCH: Expected {hex(expected).upper()}, got {hex_result}")
                    return ValidatorResult(
                        passed=False,
                        error_msg=f"MMIO Mismatch (Expected {hex(expected).upper()})",
                        context={"mmio_raw": hex(raw_val), "mmio_masked": hex_result}
                    )

            return ValidatorResult(
                passed=True,
                context={"mmio_val": hex_result}
            )

        except ValueError:
            logger.error(f"[MMIO] Invalid devmem output: {res.stdout}")
            return ValidatorResult(passed=False, error_msg="Failed to parse devmem output.")
