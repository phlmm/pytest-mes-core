import pytest
import anyio
from unittest.mock import MagicMock, AsyncMock

from pytest_mes_core.transports.base import CommandResult
from pytest_mes_core.transports.failover import FailoverTransport
from pytest_mes_core.transports.chunking import HostSideBuffer
from pytest_mes_core.transports.watchdog import UartKernelWatchdog

@pytest.mark.anyio
async def test_failover_transport_async_methods():
    primary = MagicMock()
    fallback = MagicMock()
    primary.is_connected = False
    fallback.is_connected = False
    
    # Mock the async methods directly
    primary.async_connect = AsyncMock()
    fallback.async_connect = AsyncMock()
    primary.async_disconnect = AsyncMock()
    fallback.async_disconnect = AsyncMock()
    
    mock_res = CommandResult(command="test", stdout="ok", stderr="", exited=0, ok=True, duration_s=0.1)
    primary.async_safe_run = AsyncMock(return_value=mock_res)
    fallback.async_safe_run = AsyncMock(return_value=mock_res)
    
    router = FailoverTransport(primary, fallback)
    
    # Test connect
    await router.async_connect()
    primary.async_connect.assert_awaited_once()
    fallback.async_connect.assert_awaited_once()
    
    # Test safe_run (primary)
    res = await router.async_safe_run("test")
    assert res.ok
    primary.async_safe_run.assert_awaited_once_with("test", 30.0, False, False)
    
    # Test disconnect
    await router.async_disconnect()
    primary.async_disconnect.assert_awaited_once()
    fallback.async_disconnect.assert_awaited_once()

@pytest.mark.anyio
async def test_host_side_buffer_async_methods():
    transport = MagicMock()
    buffer = HostSideBuffer(transport, "/var/log/syslog", poll_interval_s=0.5)
    
    await buffer.async_start()
    
    # Since start() spins a thread, wait a tiny bit and then stop
    await anyio.sleep(0.1)
    
    data = await buffer.async_stop()
    assert isinstance(data, list)

@pytest.mark.anyio
async def test_watchdog_async_methods():
    serial = MagicMock()
    import queue, re
    serial.is_connected = True
    test_q = queue.Queue()
    serial.subscribe.return_value = test_q
    serial.ANSI_ESCAPE_B = re.compile(rb'\x1b\[[0-9;]*[a-zA-Z]')
    watchdog = UartKernelWatchdog(serial)
    
    await watchdog.async_start()
    
    await anyio.sleep(0.1)
    
    await watchdog.async_stop()
    assert not watchdog.is_panicked()
