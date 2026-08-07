import pytest
from unittest.mock import MagicMock, patch
from pytest_mes_core.config.instruments import UsbSdMuxConfig
from pytest_mes_core.host_adapters.base import HostAdapterError
from pytest_mes_core.host_adapters.sd_mux import HostUsbSdMuxAdapter

def _make_cfg(**overrides):
    defaults = dict(serial_id='/dev/sda', host_block_device='/dev/sda')
    defaults.update(overrides)
    return UsbSdMuxConfig(**defaults)

def test_module_imports_and_resolve_device_path_globs(monkeypatch):
    """Sanity check: the module imports cleanly (no orphaned unreachable code
    referencing `self` in a staticmethod) and _resolve_device_path globs
    correctly for a non-/dev/ serial id."""
    import glob as _glob
    monkeypatch.setattr(_glob, 'glob', lambda pattern: ['/dev/usb-sd-mux/id-00048.00717'])
    resolved = HostUsbSdMuxAdapter._resolve_device_path('00048.00717')
    assert resolved == '/dev/usb-sd-mux/id-00048.00717'

def test_resolve_device_path_falls_back_when_no_match(monkeypatch):
    import glob as _glob
    monkeypatch.setattr(_glob, 'glob', lambda pattern: [])
    resolved = HostUsbSdMuxAdapter._resolve_device_path('1781')
    assert resolved == '/dev/usb-sd-mux/id-1781'

def test_enter_releases_mutex_when_set_mux_state_fails():
    cfg = _make_cfg(serial_id='/dev/fake-sd-mux')
    adapter = HostUsbSdMuxAdapter(cfg)
    mutex_cm = MagicMock()
    mutex_cm.__enter__.return_value = None
    mutex_cm.__exit__.return_value = False
    with patch('pytest_mes_core.host_adapters.sd_mux.hardware_mutex', return_value=mutex_cm), patch.object(HostUsbSdMuxAdapter, '_set_mux_state', side_effect=HostAdapterError('device unplugged')):
        with pytest.raises(HostAdapterError):
            adapter.__enter__()
    mutex_cm.__enter__.assert_called_once()
    mutex_cm.__exit__.assert_called_once()
    assert adapter._mutex_context is None