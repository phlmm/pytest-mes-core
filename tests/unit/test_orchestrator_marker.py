import pytest

from pytest_mes_core.plugins.orchestrator import _resolve_target_state_name
from pytest_mes_core.state_machine import DutState


def _marker(*args):
    @pytest.mark.requires_state(*args)
    def _fn():
        pass

    return _fn.pytestmark[0]


def test_resolve_target_state_name_with_enum_member():
    marker = _marker(DutState.BOOTLOADER)
    assert _resolve_target_state_name(marker) == "BOOTLOADER"


def test_resolve_target_state_name_with_bare_string():
    """Natural user syntax @pytest.mark.requires_state("BOOTLOADER") must not
    raise AttributeError (strings have no .name attribute)."""
    marker = _marker("BOOTLOADER")
    assert _resolve_target_state_name(marker) == "BOOTLOADER"


def test_resolve_target_state_name_no_args_defaults_to_os_userland():
    marker = _marker()
    assert _resolve_target_state_name(marker) == "OS_USERLAND"


def test_resolve_target_state_name_none_marker_defaults_to_os_userland():
    assert _resolve_target_state_name(None) == "OS_USERLAND"
