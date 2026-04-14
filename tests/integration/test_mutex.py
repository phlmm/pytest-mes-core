# tests/integration/test_mutex.py
import time
import multiprocessing
import pytest
import sys
from pytest_mes_core.host_adapters.mutex import hardware_mutex, HostMutexTimeoutError

if sys.platform != "linux":
    pytest.skip("fcntl mutex tests require Linux", allow_module_level=True)

def _worker_lock(resource: str, hold_time: float):
    """A parallel OS process attempting to steal a hardware USB lock."""
    with hardware_mutex(resource, timeout_s=1.0):
        time.sleep(hold_time)

def test_hardware_mutex_strictly_serializes_processes():
    """
    Proves that fcntl file locks strictly serialize hardware access
    across completely isolated OS processes.
    """
    resource = "test_shared_ftdi_cable"

    # Process A grabs the hardware lock and holds it for 2 seconds.
    p1 = multiprocessing.Process(target=_worker_lock, args=(resource, 2.0))
    p1.start()

    time.sleep(0.2) # Ensure p1 reaches the lock first

    # Process B tries to grab it immediately, but has a strict 1-second timeout.
    # It MUST fail and raise the Domain Exception!
    p2 = multiprocessing.Process(target=_worker_lock, args=(resource, 1.0))
    p2.start()

    p2.join()
    p1.join()

    # MATHEMATICAL PROOF:
    # Process 1 exited cleanly (0).
    # Process 2 died violently (1) because the OS kernel blocked its access!
    assert p1.exitcode == 0
    assert p2.exitcode != 0
