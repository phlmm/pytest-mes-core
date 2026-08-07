import functools
import pytest
import anyio
from unittest.mock import MagicMock, MagicMock
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
    primary.connect = MagicMock()
    fallback.connect = MagicMock()
    primary.disconnect = MagicMock()
    fallback.disconnect = MagicMock()
    mock_res = CommandResult(command='test', stdout='ok', stderr='', exited=0, ok=True, duration_s=0.1)
    primary.safe_run = MagicMock(return_value=mock_res)
    fallback.safe_run = MagicMock(return_value=mock_res)
    router = FailoverTransport(primary, fallback)
    router.connect()
    primary.connect.assert_called_once()
    fallback.connect.assert_called_once()
    res = router.safe_run('test')
    assert res.ok
    primary.safe_run.assert_called_once_with('test', 30.0, False, False)
    router.disconnect()
    primary.disconnect.assert_called_once()
    fallback.disconnect.assert_called_once()

@pytest.mark.anyio
async def test_host_side_buffer_async_methods():
    transport = MagicMock()
    buffer = HostSideBuffer(transport, '/var/log/syslog', poll_interval_s=0.5)
    buffer.start()
    await anyio.sleep(0.1)
    data = buffer.stop()
    assert isinstance(data, list)

@pytest.mark.anyio
async def test_watchdog_async_methods():
    serial = MagicMock()
    import queue, re
    serial.is_connected = True
    test_q = queue.Queue()
    serial.subscribe.return_value = test_q
    serial.ANSI_ESCAPE_B = re.compile(b'\\x1b\\[[0-9;]*[a-zA-Z]')
    watchdog = UartKernelWatchdog(serial)
    watchdog.start()
    await anyio.sleep(0.1)
    watchdog.stop()
    assert not watchdog.is_panicked()