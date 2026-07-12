from pytest_mes_core.telemetry.base import StationContext, TestRecord
from pytest_mes_core.telemetry.receipt_exporter import OperatorReceiptExporter


def _make_context(**overrides):
    kwargs = dict(jig_id="JIG-1", operator_id="OP-1")
    kwargs.update(overrides)
    return StationContext(**kwargs)


def test_receipt_counts_by_outcome(tmp_path):
    exporter = OperatorReceiptExporter(base_log_dir=tmp_path)
    exporter.start_session(_make_context())

    exporter.emit_record(TestRecord(test_name="t_pass", passed=True, outcome="passed"))
    exporter.emit_record(TestRecord(test_name="t_fail", passed=False, outcome="failed"))
    exporter.emit_record(TestRecord(test_name="t_skip", passed=False, outcome="skipped"))
    # "unknown" outcome preserves old behavior: not record.passed => failed
    exporter.emit_record(TestRecord(test_name="t_unknown_fail", passed=False, outcome="unknown"))
    exporter.emit_record(TestRecord(test_name="t_unknown_pass", passed=True, outcome="unknown"))

    assert exporter.total_tests == 5
    assert exporter.skipped_tests == 1
    assert exporter.failed_tests == 2

    exporter.end_session(session_passed=False)

    receipt_files = list((tmp_path / "operator_receipts").rglob("*.txt"))
    assert len(receipt_files) == 1
    body = receipt_files[0].read_text()
    assert "Total Tests  : 5" in body
    assert "Skipped Tests : 1" in body
    assert "Failed Tests : 2" in body
    # Skipped Tests line must sit between Total and Failed
    assert body.index("Total Tests") < body.index("Skipped Tests") < body.index("Failed Tests")


def test_receipt_skipped_test_does_not_count_as_failed(tmp_path):
    exporter = OperatorReceiptExporter(base_log_dir=tmp_path)
    exporter.start_session(_make_context())

    exporter.emit_record(TestRecord(test_name="t_pass", passed=True, outcome="passed"))
    exporter.emit_record(TestRecord(test_name="t_skip", passed=False, outcome="skipped"))

    exporter.end_session(session_passed=True)

    receipt_files = list((tmp_path / "operator_receipts").rglob("*.txt"))
    body = receipt_files[0].read_text()
    assert "Failed Tests : 0" in body
    assert "Skipped Tests : 1" in body
