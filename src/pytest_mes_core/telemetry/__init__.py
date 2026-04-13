# src/pytest_mes_core/telemetry/__init__.py

"""
MES Core Telemetry Layer
------------------------
Data routing, serialization, and external database integration.
Guarantees test suites never crash due to network database outages.
"""

# 1. Core Contracts & Data Structures
from .base import (
    StationContext,
    TestRecord,
    TelemetryExporter,
    TelemetryError,
    TelemetryDeliveryError,
    TelemetrySerializationError
)

# 2. Data Sinks (Exporters)
from .jsonl_exporter import JsonlTelemetryExporter

# Note: Future exporters (InfluxDB, REST APIs) will be imported here.
# from .influx_exporter import InfluxDbExporter
# from .mes_rest_api import MesRestApiExporter


# ==========================================
# STRICT PUBLIC API BOUNDARY
# ==========================================
__all__ = [
    # Contracts & Data
    "StationContext",
    "TestRecord",
    "TelemetryExporter",

    # Domain Exceptions
    "TelemetryError",
    "TelemetryDeliveryError",
    "TelemetrySerializationError",

    # Active Sinks
    "JsonlTelemetryExporter"
]
