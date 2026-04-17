# src/pytest_mes_core/telemetry/__init__.py

"""
MES Core Telemetry Layer
------------------------
Data routing, serialization, and external database integration.
Guarantees test suites never crash due to network database outages.
"""

# Note: Future exporters (InfluxDB, REST APIs) will be imported here.
# from .influx_exporter import InfluxDbExporter
# from .mes_rest_api import MesRestApiExporter


# ==========================================
# STRICT PUBLIC API BOUNDARY
# ==========================================
from .base import (
    StationContext,
    TestRecord,
    TelemetryExporter,
    TelemetryError,
    TelemetryDeliveryError,
    TelemetrySerializationError
)
from .jsonl_exporter import JsonlTelemetryExporter
from .receipt_exporter import OperatorReceiptExporter
from .markdown_exporter import DeveloperMarkdownExporter
from .composite import CompositeTelemetryExporter

__all__ = [
    # Contracts & Data
    "StationContext",
    "TestRecord",
    # Domain Exceptions
    "TelemetryError",
    "TelemetryDeliveryError",
    "TelemetrySerializationError",
    # Active Sinks
    "TelemetryExporter",
    "JsonlTelemetryExporter",
    "OperatorReceiptExporter",
    "DeveloperMarkdownExporter",
    "CompositeTelemetryExporter"
]
