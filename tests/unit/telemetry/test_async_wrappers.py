from unittest.mock import patch

import pytest

from pytest_mes_core.protocols.base import ValidatorResult
from pytest_mes_core.telemetry.base import StationContext, TestRecord
from pytest_mes_core.telemetry.composite import CompositeTelemetryExporter
from pytest_mes_core.telemetry.jsonl_exporter import JsonlTelemetryExporter
from pytest_mes_core.telemetry.post_mortem import JtagCrashDumper


def _make_context(**overrides):
    kwargs = dict(jig_id="JIG-1", operator_id="OP-1")
    kwargs.update(overrides)
    return StationContext(**kwargs)


@pytest.mark.anyio
async def test_async_absorb_with_default_prefix_omitted():
    """Fix 3: anyio.to_thread.run_sync does not forward **kwargs, so the sync
    default for `prefix` must be promoted onto the async signature itself."""
    record = TestRecord(test_name="test_absorb")
    validator_res = ValidatorResult(passed=True, metrics={"voltage": 3.3}, context={"note": "ok"})

    # No TypeError even though prefix is omitted (relies on the async default).
    await record.async_absorb(validator_res)

    assert record.metrics["voltage"] == 3.3
    assert record.context["note"] == "ok"


@pytest.mark.anyio
async def test_async_execute_hardware_dump_with_no_args():
    """Fix 3: both dcc_addr/stack_addr are optional; calling with no args must
    not raise a TypeError, and _send_rpc should still be reachable."""
    dumper = JtagCrashDumper(rpc_port=6666)

    with patch.object(JtagCrashDumper, "_send_rpc", return_value="mock-reg-dump") as mock_rpc:
        result = await dumper.async_execute_hardware_dump()

    assert result["registers"] == "mock-reg-dump"
    # halt + reg, no dcc/stack reads since addrs were omitted
    assert mock_rpc.call_count == 2
    assert "dcc_console" not in result
    assert "raw_stack" not in result


@pytest.mark.anyio
async def test_composite_async_emit_record_round_trip(tmp_path):
    """One composite async_emit_record round-trip must not raise TypeError."""
    jsonl_exporter = JsonlTelemetryExporter(base_log_dir=tmp_path)
    composite = CompositeTelemetryExporter(exporters=[jsonl_exporter])

    await composite.async_start_session(_make_context())
    record = TestRecord(test_name="test_roundtrip", passed=True, outcome="passed")
    await composite.async_emit_record(record)
    await composite.async_end_session(True)

    lines = jsonl_exporter.active_file.read_text().splitlines()
    assert len(lines) == 1
    assert "test_roundtrip" in lines[0]
