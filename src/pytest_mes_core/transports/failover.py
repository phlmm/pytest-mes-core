import logging
import threading
from typing import Any
from pytest_mes_core.transports.base import DutTransport, CommandResult, TransportConnectionError

logger = logging.getLogger("mes_core.transports.failover")

class FailoverTransport(DutTransport):
    """
    Reactive Failover Matrix.
    Wraps two transports. If the primary shatters, it permanently shifts
    to the secondary for the remainder of its lifecycle.

    Explicitly implements the DutTransport protocol to enable proper static
    type checking across the framework.
    """
    def __init__(self, primary: DutTransport, fallback: DutTransport):
        self.primary = primary
        self.fallback = fallback
        self.is_failed_over = False
        self._recovery_thread = None
        self._stop_recovery = threading.Event()

        if hasattr(self.fallback, "watchdog") and getattr(self.fallback, "watchdog", None):
            self.fallback.watchdog.register_panic_callback(self._on_panic)

    def _probe_primary_recovery(self) -> None:
        while not self._stop_recovery.is_set():
            if self.is_failed_over:
                try:
                    if not self.primary.is_connected:
                        self.primary.connect()
                    res = self.primary.safe_run("echo MES_PING", timeout_s=2.0)
                    if res.ok and "MES_PING" in res.stdout:
                        logger.info("[Router] HIGH-SPEED RECOVERY: Primary transport recovered! Failing-back.")
                        self.is_failed_over = False
                except Exception:
                    pass
            self._stop_recovery.wait(timeout=5.0)

    def _on_panic(self) -> None:
        logger.critical("[Router] Watchdog detected panic! Severing Primary connection to fail fast...")
        if self.primary.is_connected:
            self.primary.disconnect()

    @property
    def is_connected(self) -> bool:
        return self.fallback.is_connected if self.is_failed_over else self.primary.is_connected

    def connect(self) -> None:
        if not self.is_connected:
            logger.info("[Router] Arming dual-transport failover matrix...")
            self.primary.connect()
            self.fallback.connect()
            
            self._stop_recovery.clear()
            self._recovery_thread = threading.Thread(target=self._probe_primary_recovery, daemon=True)
            self._recovery_thread.start()
            
            logger.debug("[Router] Primary and Fallback transports bound and active.")

    def disconnect(self) -> None:
        self._stop_recovery.set()
        if self._recovery_thread and self._recovery_thread.is_alive():
            self._recovery_thread.join(timeout=1.0)
            
        logger.debug("[Router] ZERO-LEAKAGE: Tearing down dual-transport matrix.")
        self.primary.disconnect()
        self.fallback.disconnect()

    def safe_run(
        self,
        cmd: str,
        timeout_s: float = 30.0,
        check_exit_code: bool = False,
        auto_retry: bool = False,
        **kwargs: Any
    ) -> CommandResult:

        # If we already failed over earlier in this session, stay on fallback
        if self.is_failed_over:
            logger.debug("[Router] Routing via Fallback Transport...")
            return self.fallback.safe_run(cmd, timeout_s, check_exit_code, auto_retry, **kwargs)

        try:
            # Attempt Primary (SSH)
            return self.primary.safe_run(cmd, timeout_s, check_exit_code, auto_retry, **kwargs)

        except TransportConnectionError as e:
            # THE SURVIVAL EVENT: Primary Shattered physically.
            logger.critical("="*60)
            logger.critical(f"[Router] FATAL: Primary transport severed! {e}")
            logger.critical("[Router] ENGAGING OUT-OF-BAND HARDWARE FALLBACK...")
            logger.critical("="*60)

            self.is_failed_over = True

            # Send a carriage return to wake up the serial console login prompt if sleeping
            logger.debug("[Router] Transmitting wake-up pulse to fallback console...")
            self.fallback.safe_run("\n", timeout_s=1.0, check_exit_code=False)

            # 🚨 IDEMPOTENCY GUARD: Only auto-retry if explicitly marked safe
            if auto_retry:
                logger.info(f"[Router] Hardware Failover successful. Retrying idempotent command: '{cmd}'")
                return self.fallback.safe_run(cmd, timeout_s, check_exit_code, auto_retry, **kwargs)
            else:
                logger.warning(f"[Router] Failover successful, but command '{cmd}' lacks auto_retry=True. Escalating failure to FSM.")
                raise # Let the FSM catch it, mark the board DIRTY, and force a hard reboot
