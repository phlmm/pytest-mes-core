# src/pytest_mes_core/protocols/environment.py
import logging
from typing import List
from pytest_mes_core.networking import EphemeralSSHClient
from pytest_mes_core.protocols.base import ValidatorResult

logger = logging.getLogger("mes_core.protocols.env")

class EnvironmentValidator:
    """Validates the DUT's embedded Linux userland against the framework's requirements."""

    # The definitive list of required Yocto/Debian packages on the target
    REQUIRED_BINARIES = [
        "iperf3", "ethtool", "ip",
        "cansend", "candump",
        "i2ctransfer", "i2cdetect",
        "gpiomon", "gpioset", "gpioget",
        "flash_erase", "openssl",
        "dd", "base64", "hexdump", "sha256sum", "killall", "devmem"
    ]

    @staticmethod
    def verify_target_dependencies(dut_ssh: EphemeralSSHClient) -> ValidatorResult:
        """
        Executes a zero-overhead, single-shot batch check of all required binaries.
        """
        logger.info("[Pre-Flight] Verifying DUT embedded Linux dependencies...")

        # We use a pure POSIX shell loop. 'command -v' is safer than 'which'.
        # This script echoes the name of any binary it CANNOT find.
        bash_script = (
            f"for cmd in {' '.join(EnvironmentValidator.REQUIRED_BINARIES)}; do "
            "command -v $cmd >/dev/null 2>&1 || echo $cmd; "
            "done"
        )

        res = dut_ssh.safe_run(bash_script, timeout_s=5.0)

        if not res.ok:
            logger.error(f"[Pre-Flight] Dependency check failed to execute. OS corrupted? {res.stderr}")
            return ValidatorResult(passed=False, error_msg="Failed to execute dependency check.")

        # If stdout has data, those are the missing binaries
        missing_binaries = res.stdout.strip().split()

        if missing_binaries:
            error_str = ", ".join(missing_binaries)
            logger.critical(f"[Pre-Flight] FATAL: DUT firmware is missing required tools: {error_str}")
            logger.critical("Check your Yocto/Buildroot image configuration!")
            return ValidatorResult(passed=False, error_msg=f"Missing target binaries: {error_str}")

        logger.info("[Pre-Flight] All required binaries found. Environment is pristine.")
        return ValidatorResult(passed=True)
