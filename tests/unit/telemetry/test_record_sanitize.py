import json
import socket
from pytest_mes_core.telemetry.base import TestRecord, StationContext
from pytest_mes_core.telemetry.jsonl_exporter import JsonlTelemetryExporter

def _make_context(**overrides):
    kwargs = dict(jig_id='JIG-1', operator_id='OP-1')
    kwargs.update(overrides)
    return StationContext(**kwargs)

def test_post_construction_mutation_is_sanitized_before_jsonl_dump(tmp_path):
    """Fix 1: objects stuffed into context/metrics *after* construction (the
    realistic path: fixtures, absorb(), tests) must not crash the JSONL dump.
    """
    record = TestRecord(test_name='test_sock_survives')
    record.context['sock'] = socket.socket()
    record.metrics['x'] = object()
    record.context['l'] = [object()]
    exporter = JsonlTelemetryExporter(base_log_dir=tmp_path)
    exporter.start_session(_make_context())
    exporter.emit_record(record)
    lines = exporter.active_file.read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert isinstance(data['context']['sock'], str)
    assert 'socket' in data['context']['sock'].lower() or 'socket' in data['context']['sock']
    assert isinstance(data['metrics']['x'], str)
    assert isinstance(data['context']['l'], list)
    assert isinstance(data['context']['l'][0], str)

def test_primitives_survive_unstringified(tmp_path):
    record = TestRecord(test_name='test_primitives')
    record.context['nested'] = {'a': 1, 'b': [1, 2, 'three'], 'c': None}
    record.context['flag'] = True
    record.metrics['temp'] = 42.5
    record.metrics['count'] = 3
    exporter = JsonlTelemetryExporter(base_log_dir=tmp_path)
    exporter.start_session(_make_context())
    exporter.emit_record(record)
    data = json.loads(exporter.active_file.read_text().splitlines()[0])
    assert data['context']['nested'] == {'a': 1, 'b': [1, 2, 'three'], 'c': None}
    assert data['context']['flag'] is True
    assert data['metrics']['temp'] == 42.5
    assert data['metrics']['count'] == 3

def test_construction_time_sanitize_still_works():
    """Existing behavior: passing a non-serializable object directly in the
    constructor call is still sanitized by the mode='after' validator."""
    record = TestRecord(test_name='test_ctor', context={'sock': socket.socket()})
    assert isinstance(record.context['sock'], str)