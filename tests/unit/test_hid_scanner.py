import pytest
from unittest.mock import MagicMock

from pytest_mes_core.config.instruments import HidScannerConfig
from pytest_mes_core.host_adapters import hid_scanner as hid_scanner_mod
from pytest_mes_core.host_adapters.hid_scanner import (
    HeadlessBarcodeScanner,
    HidScannerTimeoutError,
)


def _make_cfg(**overrides):
    defaults = dict(scan_timeout_s=5)
    defaults.update(overrides)
    return HidScannerConfig(**defaults)


class _FakeKey:
    def __init__(self, keycode):
        self.keycode = keycode


class _FakeEvent:
    def __init__(self, keycode):
        self.type = 1  # EV_KEY
        self.value = 1
        self.keycode = keycode


def _fake_categorize(event):
    return _FakeKey(event.keycode)


def test_wait_for_scan_total_deadline_enforced(monkeypatch):
    """Stray keystrokes must not restart the full timeout on every select()
    wakeup -- a hard wall-clock deadline must still fire."""
    cfg = _make_cfg(scan_timeout_s=1)
    scanner = HeadlessBarcodeScanner(cfg)
    scanner.device = MagicMock()
    scanner.device.fd = 99

    monkeypatch.setattr(hid_scanner_mod, "ecodes", MagicMock(EV_KEY=1))
    monkeypatch.setattr(hid_scanner_mod, "categorize", _fake_categorize)

    # Wall clock advances by 0.3s on every perf_counter() call (one call per
    # loop iteration to recompute the remaining budget). Stray keystrokes
    # keep arriving (select returns ready) for as long as the simulated
    # clock is still inside the 1.0s total deadline; once it passes, select
    # naturally returns empty (no more data before the -- now tiny --
    # per-call timeout elapses), and the total-deadline check must fire.
    fake_time = [0.0]

    def fake_perf_counter():
        fake_time[0] += 0.3
        return fake_time[0]

    def fake_select(rlist, wlist, xlist, timeout):
        if fake_time[0] < 1.0:
            return ([scanner.device.fd], [], [])
        return ([], [], [])

    scanner.device.read.return_value = [_FakeEvent("KEY_1")]

    monkeypatch.setattr(hid_scanner_mod.time, "perf_counter", fake_perf_counter)
    monkeypatch.setattr(hid_scanner_mod.select, "select", fake_select)

    with pytest.raises(HidScannerTimeoutError):
        scanner.wait_for_scan()


def test_wait_for_scan_kpenter_terminates(monkeypatch):
    """Numeric-keypad Enter (KEY_KPENTER) must terminate a scan, same as KEY_ENTER."""
    cfg = _make_cfg(scan_timeout_s=5)
    scanner = HeadlessBarcodeScanner(cfg)
    scanner.device = MagicMock()
    scanner.device.fd = 99

    monkeypatch.setattr(hid_scanner_mod, "ecodes", MagicMock(EV_KEY=1))
    monkeypatch.setattr(hid_scanner_mod, "categorize", _fake_categorize)

    scanner.device.read.return_value = [
        _FakeEvent("KEY_1"),
        _FakeEvent("KEY_2"),
        _FakeEvent("KEY_KPENTER"),
    ]

    monkeypatch.setattr(
        hid_scanner_mod.select, "select", lambda *a, **kw: ([scanner.device.fd], [], [])
    )

    barcode = scanner.wait_for_scan()
    assert barcode == "12"


def test_enter_closes_non_matching_devices(monkeypatch):
    """__enter__ must close every InputDevice handle it opens that doesn't
    match, not just leave them dangling as leaked fds."""
    cfg = _make_cfg(device_name_substring="Target Scanner")
    scanner = HeadlessBarcodeScanner(cfg)

    monkeypatch.setattr(hid_scanner_mod, "HAS_EVDEV", True)

    matching = MagicMock()
    matching.name = "Target Scanner XYZ"
    matching.read_one.return_value = None

    non_matching_1 = MagicMock()
    non_matching_1.name = "Some Other Keyboard"

    non_matching_2 = MagicMock()
    non_matching_2.name = "Another Irrelevant Device"

    devices_by_path = {
        "/dev/input/event0": non_matching_1,
        "/dev/input/event1": non_matching_2,
        "/dev/input/event2": matching,
    }

    monkeypatch.setattr(
        hid_scanner_mod, "list_devices", lambda: list(devices_by_path.keys())
    )
    monkeypatch.setattr(
        hid_scanner_mod, "InputDevice", lambda path: devices_by_path[path]
    )

    scanner.__enter__()

    assert scanner.device is matching
    non_matching_1.close.assert_called_once()
    non_matching_2.close.assert_called_once()
    matching.close.assert_not_called()
