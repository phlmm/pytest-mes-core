import time
import pytest
from pytest_mes_core.transports.failover import FailoverTransport
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError
from tests.mocks.virtual_transport import MockTransport

def test_failover_router_recovers_primary_link():
    """
    Edge Case: Tests the self-healing '_probe_primary_recovery' thread in FailoverTransport.
    Proves that if a primary link (like SSH) temporarily drops (triggering a fallback to UART),
    the background thread will automatically detect when SSH comes back online and seamlessly
    route traffic back to the high-speed link without interrupting the test.
    """
    # 1. Start with a broken primary transport
    primary_behavior = {
        r"dmesg": TransportConnectionError("Connection timed out"),
        r"MES_PING": TransportConnectionError("Still offline")
    }
    mock_primary = MockTransport(primary_behavior)
    mock_primary._connected = False
    
    # 2. Start with a robust fallback transport
    mock_fallback = MockTransport({
        r"dmesg": CommandResult("dmesg", "UART Output", "", 0, True, 0.1)
    })
    mock_fallback._connected = False
    
    matrix = FailoverTransport(primary=mock_primary, fallback=mock_fallback)
    matrix.connect()
    
    # 3. Trigger failover
    res = matrix.safe_run("dmesg", auto_retry=True)
    assert res.stdout == "UART Output"
    assert matrix.is_failed_over is True
    assert "dmesg" in mock_fallback.command_history
    
    # 4. Magically "heal" the primary transport so the background thread detects it
    mock_primary.behavior_map = {
        r"MES_PING": CommandResult("echo MES_PING", "MES_PING", "", 0, True, 0.1),
        r"cat /etc/os-release": CommandResult("cat /etc/os-release", "Linux", "", 0, True, 0.1)
    }
    
    # Give the background thread time to poll and heal
    for _ in range(20):
        if not matrix.is_failed_over:
            break
        time.sleep(0.5)
        
    # 5. Assert it healed back to primary
    assert matrix.is_failed_over is False
    
    # 6. Prove traffic is routing to primary again
    res2 = matrix.safe_run("cat /etc/os-release")
    assert res2.stdout == "Linux"
    assert "cat /etc/os-release" in mock_primary.command_history
    
    matrix.disconnect()
