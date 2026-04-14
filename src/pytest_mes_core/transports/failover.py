# src/pytest_mes_core/transports/failover.py
import logging
from typing import Any
from pytest_mes_core.transports.base import DutTransport, CommandResult, TransportConnectionError

logger = logging.getLogger("mes_core.transports.failover")

class FailoverTransport:
    """
    Reactive Failover Matrix.
    Wraps two transports. If the primary shatters, it permanently shifts
    to the secondary for the remainder of its lifecycle.
    """
    def __init__(self, primary: DutTransport, fallback: DutTransport):
        self.primary = primary
        self.fallback = fallback
        self.is_failed_over = False

    @property
    def is_connected(self) -> bool:
        return self.fallback.is_connected if self.is_failed_over else self.primary.is_connected

    def connect(self) -> None:
        if not self.is_connected:
            logger.info("[Router] Arming dual-transport failover matrix...")
            self.primary.connect()
            self.fallback.connect()
            logger.debug("[Router] Primary and Fallback transports bound and active.")

    def disconnect(self) -> None:
        logger.debug("[Router] ZERO-LEAKAGE: Tearing down dual-transport matrix.")
        self.primary.disconnect()
        self.fallback.disconnect()

    def safe_run(self, cmd: str, timeout_s: float = 30.0, **kwargs: Any) -> CommandResult:
        # If we already failed over during this test session, stay on the fallback permanently
        if self.is_failed_over:
            logger.debug("[Router] Routing via Fallback Transport...")
            return self.fallback.safe_run(cmd, timeout_s, **kwargs)

        try:
            # 1. Attempt Primary
            return self.primary.safe_run(cmd, timeout_s, **kwargs)

        except TransportConnectionError as e:
            # 2. THE SURVIVAL EVENT: Primary Shattered physically.
            # Note: The "silent socket closure" is elegantly handled inside
            # primary.safe_run() which raises this exact error.
            logger.critical("="*60)
            logger.critical(f"[Router] FATAL: Primary transport severed! {e}")
            logger.critical("[Router] ENGAGING OUT-OF-BAND HARDWARE FALLBACK...")
            logger.critical("="*60)

            self.is_failed_over = True

            # Send a carriage return to wake up the serial console login prompt if sleeping
            logger.debug("[Router] Transmitting wake-up pulse to fallback console...")
            self.fallback.safe_run("\n", timeout_s=1.0, check_exit_code=False)

            # Retry the exact command over the unkillable line
            logger.info(f"[Router] Hardware Failover successful. Retrying command: '{cmd}'")
            return self.fallback.safe_run(cmd, timeout_s, **kwargs)
