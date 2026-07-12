# tests/unit/test_probe_rs_client.py
"""
Unit tests for Fix 9: ProbeRsTransport.halt() contract honesty.

The stateless probe-rs CLI cannot actually halt the core -- halt() issues a
plain reset and the MCU resumes running immediately. These tests lock in
that halt() still performs the reset (unchanged behavior) while now emitting
an honest "probe_rs_halt_unsupported" warning instead of a misleading
"reset --halt" docstring/log claim.

subprocess.run is mocked -- no probe-rs binary or hardware required.
"""
from unittest.mock import MagicMock, patch

import structlog

from pytest_mes_core.config.instruments import ProbeRsTargetConfig
from pytest_mes_core.transports.probe_rs_client import ProbeRsTransport


def _make_transport() -> ProbeRsTransport:
    cfg = ProbeRsTargetConfig(chip="STM32F407VG", protocol="swd", speed=4000)
    transport = ProbeRsTransport(cfg)
    transport._probe_rs_path = "/usr/bin/probe-rs"  # skip _resolve_binary's shutil.which
    return transport


def _ok_result() -> MagicMock:
    r = MagicMock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


class TestHaltHonesty:
    def test_halt_issues_plain_reset_not_reset_halt(self):
        """halt() must still invoke a plain `reset` -- probe-rs has no
        standalone halt/reset --halt subcommand to call instead."""
        transport = _make_transport()
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            transport.halt()
        cmd = mock_run.call_args[0][0]
        assert "reset" in cmd
        assert "--halt" not in cmd

    def test_halt_emits_unsupported_warning(self):
        """halt() must warn that the core will RUN, not remain halted --
        the old docstring's "reset --halt ... halt mode" claim was false."""
        transport = _make_transport()
        events = []
        with structlog.testing.capture_logs() as cap_logs:
            with patch("subprocess.run", return_value=_ok_result()):
                transport.halt()
            events = cap_logs

        warning_events = [e for e in events if e.get("event") == "probe_rs_halt_unsupported"]
        assert len(warning_events) == 1
        assert warning_events[0]["log_level"] == "warning"
        assert "run" in warning_events[0]["hint"].lower()

    def test_resume_issues_plain_reset(self):
        transport = _make_transport()
        with patch("subprocess.run", return_value=_ok_result()) as mock_run:
            transport.resume()
        cmd = mock_run.call_args[0][0]
        assert "reset" in cmd
