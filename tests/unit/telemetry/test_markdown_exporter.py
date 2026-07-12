from pytest_mes_core.telemetry.base import StationContext, TestRecord
from pytest_mes_core.telemetry.markdown_exporter import DeveloperMarkdownExporter


def _make_context(**overrides):
    kwargs = dict(jig_id="JIG-1", operator_id="OP-1")
    kwargs.update(overrides)
    return StationContext(**kwargs)


def test_markdown_renders_skip_icon_without_traceback(tmp_path):
    exporter = DeveloperMarkdownExporter(base_log_dir=tmp_path)
    exporter.start_session(_make_context())

    record = TestRecord(
        test_name="test_needs_psu",
        passed=False,
        outcome="skipped",
        error_message="SKIPPED: requires a physical PSU",
    )
    record.context["full_traceback"] = "Traceback (most recent call last): ..."
    exporter.emit_record(record)

    body = exporter.filepath.read_text()
    assert "⏭️ SKIP" in body
    assert "❌ FAIL" not in body
    assert "requires a physical PSU" in body
    # Skips must not render the failure traceback block.
    assert "View Full Traceback" not in body


def test_markdown_renders_fail_icon_with_traceback(tmp_path):
    exporter = DeveloperMarkdownExporter(base_log_dir=tmp_path)
    exporter.start_session(_make_context())

    record = TestRecord(
        test_name="test_broken",
        passed=False,
        outcome="failed",
        error_message="AssertionError: boom",
    )
    record.context["full_traceback"] = "Traceback (most recent call last): ..."
    exporter.emit_record(record)

    body = exporter.filepath.read_text()
    assert "❌ FAIL" in body
    assert "⏭️ SKIP" not in body
    assert "View Full Traceback" in body


def test_markdown_renders_pass_icon(tmp_path):
    exporter = DeveloperMarkdownExporter(base_log_dir=tmp_path)
    exporter.start_session(_make_context())

    record = TestRecord(test_name="test_ok", passed=True, outcome="passed")
    exporter.emit_record(record)

    body = exporter.filepath.read_text()
    assert "✅ PASS" in body
