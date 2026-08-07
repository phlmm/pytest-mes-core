import pytest
import time
from unittest.mock import MagicMock, MagicMock
from pytest_mes_core.telemetry.profiler import HardwareProfiler
from pytest_mes_core.transports.base import CommandResult

def test_async_hardware_profiler():
    dut = MagicMock()
    dut.is_connected = True
    dut.safe_run = MagicMock(return_value=CommandResult(command='', stdout='85000\n', stderr='', exited=0, ok=True, duration_s=0.1))
    psu = MagicMock()
    psu.measure_current = MagicMock(return_value=1.5)
    psu.measure_voltage = MagicMock(return_value=12.0)
    profiler = HardwareProfiler(dut=dut, psu=psu, interval_s=0.1)
    with profiler:
        time.sleep(0.35)
    assert not profiler.is_running
    assert len(profiler.metrics['temp_c']) >= 2
    assert len(profiler.metrics['psu_current_a']) >= 2
    summary = profiler.summarize()
    assert summary['peak_temp_c'] == 85.0
    assert summary['avg_temp_c'] == 85.0
    assert summary['peak_current_a'] == 1.5
    assert summary['avg_voltage_v'] == 12.0

def test_async_hardware_profiler_negative_temperature():
    dut = MagicMock()
    dut.is_connected = True
    dut.safe_run = MagicMock(return_value=CommandResult(command='', stdout='-5000\n', stderr='', exited=0, ok=True, duration_s=0.1))
    profiler = HardwareProfiler(dut=dut, interval_s=0.1)
    with profiler:
        time.sleep(0.25)
    assert len(profiler.metrics['temp_c']) >= 1
    assert all((t == -5.0 for t in profiler.metrics['temp_c']))
    summary = profiler.summarize()
    assert summary['peak_temp_c'] == -5.0
    assert summary['avg_temp_c'] == -5.0

def test_async_hardware_profiler_garbage_console_noise():
    dut = MagicMock()
    dut.is_connected = True
    dut.safe_run = MagicMock(return_value=CommandResult(command='', stdout='garbage\n', stderr='', exited=0, ok=True, duration_s=0.1))
    profiler = HardwareProfiler(dut=dut, interval_s=0.1)
    with profiler:
        time.sleep(0.25)
    assert profiler.metrics['temp_c'] == []
    summary = profiler.summarize()
    assert 'peak_temp_c' not in summary
    assert 'avg_temp_c' not in summary