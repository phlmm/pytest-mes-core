# src/pytest_mes_core/transports/failover.py
import logging
from .base import DutTransport, CommandResult, TransportConnectionError

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
        self.primary.connect()
        self.fallback.connect()

    def disconnect(self) -> None:
        self.primary.disconnect()
        self.fallback.disconnect()

    def safe_run(self, cmd: str, timeout_s: float = 30.0, **kwargs: Any) -> CommandResult:
        # If we already failed over during this test, stay on the fallback
        if self.is_failed_over:
            return self.fallback.safe_run(cmd, timeout_s, **kwargs)

        try:
            # 1. Attempt Primary
            return self.primary.safe_run(cmd, timeout_s, **kwargs)

        except TransportConnectionError as e:
            # 2. THE SURVIVAL EVENT: Primary Shattered physically.
            # Note: The "silent socket closure" is now elegantly handled inside
            # primary.safe_run() which raises this exact error.
            logger.critical(f"[Failover] Primary transport severed: {e}")
            logger.critical("[Failover] ENGAGING OUT-OF-BAND SERIAL FALLBACK...")

            self.is_failed_over = True

            # Send a carriage return to wake up the serial console login prompt if sleeping
            self.fallback.safe_run("\n", timeout_s=1.0)

            # Retry the exact command over the unkillable line
            return self.fallback.safe_run(cmd, timeout_s, **kwargs)
