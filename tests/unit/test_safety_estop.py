import time

import pytest
from unittest.mock import MagicMock, patch

from gpiod.line import Value

from pytest_mes_core.config.instruments import EStopConfig
from pytest_mes_core.host_adapters.safety import EStopWatchdog
from pytest_mes_core.plugins.core_config import _arm_estop_watchdog


def _make_cfg(**overrides):
    defaults = dict(line=7)
    defaults.update(overrides)
    return EStopConfig(**defaults)


def test_enter_arms_via_gpiod_v2_and_exit_releases():
    cfg = _make_cfg()
    watchdog = EStopWatchdog(cfg)

    request_mock = MagicMock()
    # active_low=True (default) => trigger_state=0; ACTIVE (1) is the "not pressed" rest
    # state, so the background monitor thread must not fire SIGINT during this test.
    request_mock.get_value.return_value = Value.ACTIVE

    with patch(
        "pytest_mes_core.host_adapters.safety.gpiod.request_lines",
        return_value=request_mock,
    ) as mock_request_lines:
        watchdog.__enter__()
        try:
            args, kwargs = mock_request_lines.call_args
            assert args[0] == "/dev/gpiochip0"
            assert kwargs["consumer"] == "mes_estop"
            assert set(kwargs["config"].keys()) == {7}
            assert watchdog._request is request_mock
        finally:
            watchdog.__exit__(None, None, None)

    request_mock.release.assert_called_once()


def test_monitor_triggers_sigint_on_estop_active():
    cfg = _make_cfg(active_low=True, polling_interval_s=0.01)
    watchdog = EStopWatchdog(cfg)

    request_mock = MagicMock()
    # active_low=True => trigger_state = 0; a logical Value.INACTIVE (0) maps to state 0.
    request_mock.get_value.return_value = Value.INACTIVE
    watchdog._request = request_mock

    kill_calls = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        watchdog._stop_event.set()

    with patch("pytest_mes_core.host_adapters.safety.os.kill", side_effect=fake_kill) as mock_kill, \
         patch("pytest_mes_core.host_adapters.safety.os._exit") as mock_exit, \
         patch("pytest_mes_core.host_adapters.safety.time.sleep") as mock_sleep:
        watchdog._monitor()

    assert kill_calls
    import signal
    assert kill_calls[0][1] == signal.SIGINT
    mock_sleep.assert_any_call(5.0)
    mock_exit.assert_called_once_with(1)


def test_arm_estop_watchdog_required_raises_exit():
    cfg = _make_cfg(required=True)

    class _FakeBom:
        pass

    bom = _FakeBom()
    bom.e_stop = cfg

    config = MagicMock()

    with patch.object(EStopWatchdog, "__enter__", side_effect=RuntimeError("boom")):
        with pytest.raises(pytest.exit.Exception):
            _arm_estop_watchdog(bom, config)


def test_arm_estop_watchdog_not_required_warns_without_raising():
    cfg = _make_cfg(required=False)

    class _FakeBom:
        pass

    bom = _FakeBom()
    bom.e_stop = cfg

    config = MagicMock()

    with patch.object(EStopWatchdog, "__enter__", side_effect=RuntimeError("boom")):
        _arm_estop_watchdog(bom, config)  # must not raise
