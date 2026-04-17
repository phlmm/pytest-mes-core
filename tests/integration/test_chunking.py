# tests/integration/test_chunking.py
import time
from pytest_mes_core.transports.chunking import HostSideBuffer
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError
from tests.mocks.virtual_transport import MockTransport

class ShatteringTransport(MockTransport):
    """A Digital Twin that simulates a Toradex board dying mid-transfer."""
    def __init__(self):
        super().__init__({})
        self.call_count = 0

    def safe_run(self, cmd, timeout_s=30.0, **kwargs):
        self.call_count += 1
        if self.call_count <= 2:
            return CommandResult(cmd, stdout=f"Log Line {self.call_count}\n", stderr="", exited=0, ok=True, duration_s=0.1)
        else:
            # THE SURVIVAL EVENT: Kernel Panic destroys the SSH socket
            raise TransportConnectionError("SSH Pipe shattered.")

def test_host_side_buffer_survives_shattered_socket():
    transport = ShatteringTransport()

    # 1. Arm the vacuum
    buffer = HostSideBuffer(transport, "/var/log/syslog", poll_interval_s=0.1)
    buffer.start()

    # 2. Wait enough time for it to poll twice, then hit the exception on the 3rd poll
    time.sleep(1.2)

    # 3. Stop the buffer. IF THE THREAD DEADLOCKED, THIS TEST HANGS FOREVER!
    surviving_data = buffer.stop()

    # 4. Prove it gracefully aborted and captured the pre-crash data
    assert len(surviving_data) == 2
    assert surviving_data[0] == "Log Line 1"
    assert surviving_data[1] == "Log Line 2"
