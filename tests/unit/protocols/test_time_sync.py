import pytest
from unittest.mock import MagicMock, patch
from pytest_mes_core.protocols.time_sync import RtcTimeValidator
from pytest_mes_core.config import TimeSyncConfig
from pytest_mes_core.config.base import TimeDaemonType
from pytest_mes_core.transports import CommandResult, TransportTimeoutError
import time as real_time


def test_time_sync_healthy_no_drift():
    mock_dut = MagicMock()
    cfg = TimeSyncConfig(
        max_drift_s=5.0,
        force_host_sync=True,
        daemon_type=TimeDaemonType.CHRONY,
        rtc_paths=["/sys/class/rtc/rtc0"],
        verify_hardware_pps=False,
    )

    host_epoch = real_time.time()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if "chronyc" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout="Reference ID: 192.168.1.1\nStratum: 3\n", stderr="", duration_s=0.1)
        elif "date +%s" in cmd:
            # Return epoch within 1 second of host
            return CommandResult(command=cmd, ok=True, exited=0, stdout=str(int(host_epoch)) + "\n", stderr="", duration_s=0.1)
        elif "/sys/class/rtc/rtc0/since_epoch" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout=str(int(host_epoch)) + "\n", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect

    res = RtcTimeValidator.verify_and_sync_time(mock_dut, cfg)

    assert res.passed
    assert res.context["is_temporally_tainted"] is False
    assert "host_to_dut_drift_s" in res.metrics

def test_time_sync_excessive_drift_force_sync():
    mock_dut = MagicMock()
    cfg = TimeSyncConfig(
        max_drift_s=5.0,
        force_host_sync=True,
        daemon_type=TimeDaemonType.CHRONY,
        rtc_paths=["/sys/class/rtc/rtc0"],
        verify_hardware_pps=False,
    )

    host_epoch = real_time.time()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if "chronyc" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout="Reference ID: 0.0.0.0\n", stderr="", duration_s=0.1)
        elif "date +%s" in cmd:
            # DUT is 3600 seconds behind (1 hour drift!)
            return CommandResult(command=cmd, ok=True, exited=0, stdout=str(int(host_epoch) - 3600) + "\n", stderr="", duration_s=0.1)
        elif "/sys/class/rtc/rtc0/since_epoch" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout=str(int(host_epoch)) + "\n", stderr="", duration_s=0.1)
        elif "killall" in cmd or "date -u -s" in cmd or "hwclock" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect

    res = RtcTimeValidator.verify_and_sync_time(mock_dut, cfg)

    # force_host_sync=True means it should force-sync and still pass
    assert res.passed
    assert res.context["is_temporally_tainted"] is True
    assert any("TAINTED" in w for w in res.context["temporal_warnings"])

def test_time_sync_excessive_drift_no_force_fails():
    mock_dut = MagicMock()
    cfg = TimeSyncConfig(
        max_drift_s=5.0,
        force_host_sync=False,  # Don't auto-fix
        daemon_type=TimeDaemonType.CHRONY,
        rtc_paths=[],
        verify_hardware_pps=False,
    )

    host_epoch = real_time.time()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if "chronyc" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif "date +%s" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout=str(int(host_epoch) - 3600) + "\n", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect

    res = RtcTimeValidator.verify_and_sync_time(mock_dut, cfg)

    assert not res.passed
    assert "drift" in res.error_msg.lower()

def test_time_sync_dead_rtc_battery():
    mock_dut = MagicMock()
    cfg = TimeSyncConfig(
        max_drift_s=5.0,
        force_host_sync=True,
        daemon_type=TimeDaemonType.CHRONY,
        rtc_paths=["/sys/class/rtc/rtc0"],
        verify_hardware_pps=False,
    )

    host_epoch = real_time.time()

    def safe_run_side_effect(cmd, *args, **kwargs):
        if "chronyc" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)
        elif "date +%s" in cmd:
            return CommandResult(command=cmd, ok=True, exited=0, stdout=str(int(host_epoch)) + "\n", stderr="", duration_s=0.1)
        elif "/sys/class/rtc/rtc0" in cmd:
            # RTC epoch is near 0 (1970) => dead battery
            return CommandResult(command=cmd, ok=True, exited=0, stdout="100\n", stderr="", duration_s=0.1)
        return CommandResult(command=cmd, ok=True, exited=0, stdout="", stderr="", duration_s=0.1)

    mock_dut.safe_run.side_effect = safe_run_side_effect

    res = RtcTimeValidator.verify_and_sync_time(mock_dut, cfg)

    assert res.passed  # Dead battery is a warning, not a failure
    assert any("battery" in w.lower() for w in res.context["temporal_warnings"])


def test_chrony_output_parser_json():
    raw = '{"ref_id": "192.168.1.1", "stratum": 3}'
    parsed = RtcTimeValidator._parse_chrony_output(raw)
    assert parsed["ref_id"] == "192.168.1.1"
    assert parsed["stratum"] == 3

def test_chrony_output_parser_kv_fallback():
    raw = "Reference ID : 192.168.1.1\nStratum : 3\nLast offset : +0.001s"
    parsed = RtcTimeValidator._parse_chrony_output(raw)
    assert parsed["Reference ID"] == "192.168.1.1"
    assert parsed["Stratum"] == "3"

def test_audit_daemon_ntpd():
    mock_dut = MagicMock()
    ctx = {}
    mock_dut.safe_run.return_value = CommandResult(
        command="ntpq", ok=True, exited=0, stdout="*192.168.1.1 .GPS. 1\n", stderr="", duration_s=0.1
    )

    RtcTimeValidator._audit_time_daemon(mock_dut, TimeDaemonType.NTPD, ctx)
    assert ctx["daemon_health"] == "*192.168.1.1 .GPS. 1"

def test_audit_daemon_systemd():
    mock_dut = MagicMock()
    ctx = {}
    mock_dut.safe_run.return_value = CommandResult(
        command="timedatectl", ok=True, exited=0, stdout="Server: 0.pool.ntp.org\nPoll: 64s", stderr="", duration_s=0.1
    )

    RtcTimeValidator._audit_time_daemon(mock_dut, TimeDaemonType.SYSTEMD, ctx)
    assert "Server" in ctx["daemon_health"]

def test_audit_daemon_ptp():
    mock_dut = MagicMock()
    ctx = {}
    mock_dut.safe_run.return_value = CommandResult(
        command="pmc", ok=True, exited=0, stdout="portState SLAVE", stderr="", duration_s=0.1
    )

    RtcTimeValidator._audit_time_daemon(mock_dut, TimeDaemonType.PTP, ctx)
    assert "SLAVE" in ctx["daemon_health"]
