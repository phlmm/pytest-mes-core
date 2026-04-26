import pytest
import anyio
from unittest.mock import MagicMock, AsyncMock
from pytest_mes_core.telemetry.profiler import AsyncHardwareProfiler
from pytest_mes_core.transports.base import CommandResult

@pytest.mark.anyio
async def test_async_hardware_profiler():
    # Mock DUT
    dut = MagicMock()
    dut.is_connected = True
    # Simulate a fast-polling temperature output
    dut.async_safe_run = AsyncMock(return_value=CommandResult(command="", stdout="85000\n", stderr="", exited=0, ok=True, duration_s=0.1))

    # Mock PSU
    psu = MagicMock()
    psu.measure_current = MagicMock(return_value=1.5)
    psu.measure_voltage = MagicMock(return_value=12.0)

    profiler = AsyncHardwareProfiler(dut=dut, psu=psu, interval_s=0.1)

    async with profiler:
        # Sleep to let the background poll loop run a few times
        await anyio.sleep(0.35)

    # Validate that it stopped automatically via context manager
    assert not profiler.is_running
    
    # Validate metrics were gathered without blocking
    assert len(profiler.metrics["temp_c"]) >= 2
    assert len(profiler.metrics["psu_current_a"]) >= 2
    
    # Check averages and peaks
    summary = profiler.summarize()
    assert summary["peak_temp_c"] == 85.0
    assert summary["avg_temp_c"] == 85.0
    assert summary["peak_current_a"] == 1.5
    assert summary["avg_voltage_v"] == 12.0
