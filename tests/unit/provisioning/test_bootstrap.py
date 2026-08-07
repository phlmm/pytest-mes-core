import pytest
from unittest.mock import MagicMock, patch
from gpiod.line import Value
from pytest_mes_core.config import BootstrapConfig
from pytest_mes_core.provisioning.bootstrap import HardwareBootstrapper
from pytest_mes_core.provisioning.base import ProvisioningError

def _make_cfg(**overrides):
    defaults = dict(reset_pin=5, boot_pins=[1, 2], boot_modes={'recovery': [1, 0], 'normal': [0, 0]})
    defaults.update(overrides)
    return BootstrapConfig(**defaults)

def test_strobe_hardware_requests_v2_lines_and_strobes_correctly():
    cfg = _make_cfg()
    bootstrapper = HardwareBootstrapper(cfg)
    request_mock = MagicMock()
    request_mock.__enter__.return_value = request_mock
    request_mock.__exit__.return_value = False
    with patch('pytest_mes_core.provisioning.bootstrap.gpiod.request_lines', return_value=request_mock) as mock_request_lines:
        bootstrapper.set_boot_mode('recovery')
    args, kwargs = mock_request_lines.call_args
    assert args[0] == '/dev/gpiochip0'
    assert kwargs['consumer'] == 'mes_bootstrap'
    assert set(kwargs['config'].keys()) == {1, 2, 5}
    calls = [call.args for call in request_mock.set_value.call_args_list]
    assert calls == [(1, Value.ACTIVE), (2, Value.INACTIVE), (5, Value.INACTIVE), (5, Value.ACTIVE)]

def test_strobe_hardware_reset_active_high_inverts_assert_release():
    cfg = _make_cfg(reset_active_low=False)
    bootstrapper = HardwareBootstrapper(cfg)
    request_mock = MagicMock()
    request_mock.__enter__.return_value = request_mock
    request_mock.__exit__.return_value = False
    with patch('pytest_mes_core.provisioning.bootstrap.gpiod.request_lines', return_value=request_mock):
        bootstrapper.set_boot_mode('normal')
    calls = [call.args for call in request_mock.set_value.call_args_list]
    assert calls == [(1, Value.INACTIVE), (2, Value.INACTIVE), (5, Value.ACTIVE), (5, Value.INACTIVE)]

def test_set_boot_mode_undefined_mode_raises_without_touching_gpiod():
    cfg = _make_cfg()
    bootstrapper = HardwareBootstrapper(cfg)
    with patch('pytest_mes_core.provisioning.bootstrap.gpiod.request_lines') as mock_request_lines:
        with pytest.raises(ProvisioningError, match='is not defined'):
            bootstrapper.set_boot_mode('bogus')
    mock_request_lines.assert_not_called()

def test_set_boot_mode_state_count_mismatch_raises_without_touching_gpiod():
    cfg = _make_cfg(boot_modes={'recovery': [1, 0, 1], 'normal': [0, 0]})
    bootstrapper = HardwareBootstrapper(cfg)
    with patch('pytest_mes_core.provisioning.bootstrap.gpiod.request_lines') as mock_request_lines:
        with pytest.raises(ProvisioningError, match='Mismatch'):
            bootstrapper.set_boot_mode('recovery')
    mock_request_lines.assert_not_called()

def test_strobe_hardware_maps_gpiod_failure_to_provisioning_error():
    cfg = _make_cfg()
    bootstrapper = HardwareBootstrapper(cfg)
    with patch('pytest_mes_core.provisioning.bootstrap.gpiod.request_lines', side_effect=OSError('no such device')):
        with pytest.raises(ProvisioningError, match='Failed to toggle physical bootstrap pins'):
            bootstrapper.set_boot_mode('recovery')