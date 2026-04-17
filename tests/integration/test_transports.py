# tests/integration/test_transports.py
from pytest_mes_core.transports.failover import FailoverTransport
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError
from tests.mocks.virtual_transport import MockTransport

def test_matrix_catches_ssh_drop_and_routes_to_serial():
    """
    Proves that if the primary high-speed transport drops (e.g. Kernel Panic),
    the matrix permanently shifts to the out-of-band serial port.
    """
    # 1. Create a simulated SSH transport that is programmed to violently drop
    mock_ssh = MockTransport({
        r"dmesg": TransportConnectionError("SSH Pipe Shattered: Broken pipe")
    })

    # 2. Create a simulated Serial transport that is robust
    mock_serial = MockTransport({
        r"\n": CommandResult(command="wakeup", stdout="", stderr="", exited=0, ok=True, duration_s=0.1),
        r"dmesg": CommandResult(command="dmesg", stdout="Kernel Panic Output", stderr="", exited=0, ok=True, duration_s=0.5)
    })

    matrix = FailoverTransport(primary=mock_ssh, fallback=mock_serial)
    matrix.connect()

    # 3. Execute the fatal command
    res = matrix.safe_run("dmesg", auto_retry=True)

    # 4. The Assertions
    assert matrix.is_failed_over is True
    assert res.ok is True
    assert res.stdout == "Kernel Panic Output"

    # Prove the SSH transport was called, crashed, and the Serial transport took over
    assert "dmesg" in mock_ssh.command_history
    assert "dmesg" in mock_serial.command_history
