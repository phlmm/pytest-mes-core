import logging
import time
from typing import Optional

from pytest_mes_core.transports import DutTransport
from pytest_mes_core.state_machine import EmbeddedLinuxStateMachine, DutState

logger = logging.getLogger("mes_core.protocols.swupdate")

class SWUpdateValidator:
    def __init__(self, dut: DutTransport, fsm: EmbeddedLinuxStateMachine):
        self.dut = dut
        self.fsm = fsm

    def pre_flight_checks(self) -> str:
        """Verifies the board is healthy and capable of accepting an update."""
        logger.info("[SWUpdate] Running pre-flight checks...")

        # 1. Verify SWUpdate binary exists
        self.dut.safe_run("which swupdate", timeout_s=2.0, check_exit_code=True)

        # 2. Capture the CURRENT active partition before we do anything
        original_rootfs = self.fsm.context.active_rootfs
        if original_rootfs == "UNKNOWN":
            logger.warning("[SWUpdate] WARNING: Current RootFS is UNKNOWN. A/B flip verification may be compromised.")

        logger.info(f"[SWUpdate] Pre-flight OK. Current Active Partition: {original_rootfs}")
        return original_rootfs

    def install_from_url(self, swu_url: str, timeout_s: float = 300.0) -> None:
        """
        Streams the update directly into flash via HTTP.
        Bypasses RAM/tmpfs limits and avoids slow serial transfers.
        """
        logger.info(f"[SWUpdate] Initiating direct HTTP stream from: {swu_url}")

        # -v: Verbose
        # -d: Download mode (streams to flash instead of saving to disk)
        cmd = f"swupdate -v -d '-u {swu_url}'"

        # 🚨 Use check_exit_code=True to automatically raise a RuntimeError with the exact failure!
        res = self.dut.safe_run(cmd, timeout_s=timeout_s, check_exit_code=True)

        if "Installation successful" in res.stdout:
            logger.info("[SWUpdate] Payload extracted and written to inactive partition successfully.")
        else:
            logger.warning("[SWUpdate] Command exited 0, but 'Installation successful' not found in stdout. Proceeding with caution.")

    def install_from_local_media(self, file_path: str, timeout_s: float = 300.0) -> None:
        """
        Installs an update from a locally mounted USB drive or SD Card.
        e.g., file_path = "/run/media/sda1/evse-image.swu"
        """
        logger.info(f"[SWUpdate] Initiating local install from: {file_path}")

        # Check if file actually exists before starting
        self.dut.safe_run(f"ls {file_path}", timeout_s=2.0, check_exit_code=True)

        cmd = f"swupdate -i {file_path} -v"
        self.dut.safe_run(cmd, timeout_s=timeout_s, check_exit_code=True)

        logger.info("[SWUpdate] Local payload written successfully.")

    def verify_partition_flip(self, original_rootfs: str) -> None:
        """
        Reboots the board and verifies U-Boot successfully transitioned to the new partition.
        """
        logger.info("[SWUpdate] Rebooting board to verify A/B partition flip...")

        # 1. Command the FSM to bounce the board
        self.fsm.machine.mark_dirty()
        self.fsm.machine.boot_to_os()

        # 2. Check the new context populated during the boot phase
        new_rootfs = self.fsm.context.active_rootfs

        logger.info(f"[SWUpdate] Board recovered. New Active Partition: {new_rootfs}")

        if original_rootfs != "UNKNOWN" and new_rootfs == original_rootfs:
            raise RuntimeError(f"SWUpdate Failed: Board rebooted back into the original partition ({original_rootfs}). The update was rejected by U-Boot.")

        # 3. Final verification that swupdate recognizes the new state
        res = self.dut.safe_run("swupdate -g", timeout_s=3.0, check_exit_code=False)
        if "testing" in res.stdout.lower():
            logger.info("[SWUpdate] Boot successful. Committing update permanently...")
            # If U-Boot uses a bootcounter, we must accept the update so it doesn't revert on next reboot
            self.dut.safe_run("fw_setenv upgrade_available 0", timeout_s=3.0, check_exit_code=False)
            self.dut.safe_run("fw_setenv bootcount 0", timeout_s=3.0, check_exit_code=False)

    def execute_full_ota(self, swu_url: str, timeout_s: float = 300.0) -> None:
        """Helper to run the entire sequence in one shot."""
        logger.info("="*60)
        logger.info("[SWUpdate] COMMENCING OVER-THE-AIR UPDATE")
        logger.info("="*60)

        original_part = self.pre_flight_checks()
        self.install_from_url(swu_url, timeout_s)
        self.verify_partition_flip(original_part)

        logger.info("="*60)
        logger.info("[SWUpdate] OVER-THE-AIR UPDATE COMPLETE")
        logger.info("="*60)
