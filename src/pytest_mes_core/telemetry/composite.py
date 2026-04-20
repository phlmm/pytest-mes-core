import logging
from typing import List, Optional

from pytest_mes_core.telemetry.base import (
    StationContext, TestRecord, TelemetryExporter, TelemetryDeliveryError
)

logger = logging.getLogger("mes_core.telemetry.composite")

class CompositeTelemetryExporter:
    """
    Broadcast Router.
    Implements the TelemetryExporter protocol and forwards calls to N underlying exporters.
    Strictly raises exceptions if ANY exporter fails, guaranteeing Zero-Leakage visibility.
    """
    def __init__(self, exporters: List[TelemetryExporter]):
        self.exporters = exporters

    @property
    def context(self) -> Optional[StationContext]:
        return self.exporters[0].context if self.exporters else None

    def start_session(self, context: StationContext) -> None:
        """Initializes the session on all registered exporters.

        Args:
            context: The station metadata context for this session.
        """
        for exporter in self.exporters:
            exporter.start_session(context)

    def emit_record(self, record: TestRecord) -> None:
        """Forwards the record to all registered exporters.

        Args:
            record: The test record to emit.

        Raises:
            TelemetryDeliveryError: If ANY exporter fails to deliver the payload.
        """
        errors = []
        for exporter in self.exporters:
            try:
                exporter.emit_record(record)
            except Exception as e:
                logger.error(f"[Telemetry] Router failed to emit to {type(exporter).__name__}: {e}")
                errors.append(str(e))

        # THE FIX: Do not swallow the exception!
        if errors:
            raise TelemetryDeliveryError(f"Composite router failed to deliver payload: {errors}")

    def end_session(self, session_passed: bool) -> None:
        """Finalizes the session on all registered exporters.

        Args:
            session_passed: True if all tests passed, False otherwise.

        Raises:
            TelemetryDeliveryError: If ANY exporter fails during teardown.
        """
        errors = []
        for exporter in self.exporters:
            try:
                exporter.end_session(session_passed)
            except Exception as e:
                logger.error(f"[Telemetry] Exporter {type(exporter).__name__} failed teardown: {e}")
                errors.append(str(e))

        if errors:
            raise TelemetryDeliveryError(f"Composite router failed during teardown: {errors}")
