# src/pytest_mes_core/transports/failover.py
import logging
from .base import DutTransport, CommandResult

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

    def safe_run(self, cmd: str, timeout_s: float = 30.0, **kwargs) -> CommandResult:
        # If we already failed over during this test, stay on the fallback
        if self.is_failed_over:
            return self.fallback.safe_run(cmd, timeout_s, **kwargs)

        try:
            # 1. Attempt Primary
            res = self.primary.safe_run(cmd, timeout_s, **kwargs)

            # Catch silent socket closures (Fabric/Paramiko specific behavior)
            if not res.ok and "closed" in str(res.stderr).lower():
                raise ConnectionError("Primary socket closed unexpectedly.")

            return res

        except Exception as e:
            # 2. THE SURVIVAL EVENT: Primary Shattered.
            logger.critical(f"[Failover] Primary transport severed: {e}")
            logger.critical("[Failover] ENGAGING OUT-OF-BAND SERIAL FALLBACK...")

            self.is_failed_over = True

            # Send a carriage return to wake up the serial console login prompt if sleeping
            self.fallback.safe_run("\n", timeout_s=1.0)

            # Retry the exact command over the unkillable line
            return self.fallback.safe_run(cmd, timeout_s, **kwargs)
