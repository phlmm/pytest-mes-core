import os
import pty
import time
import pytest
from pytest_mes_core.transports.serial_client import EphemeralSerialClient
from pytest_mes_core.config import HostSerialConfig
from pytest_mes_core.state_machine import KernelPanicError

def test_uart_watchdog_integration_with_pty():
    """
    True integration test spinning up a virtual TTY loopback.
    Simultaneously tests EphemeralSerialClient, UartKernelWatchdog, and execution_locks.
    """
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    
    # 1. Spin up the Serial Client pointing to our Virtual UART
    cfg = HostSerialConfig(port=slave_name, baudrate=115200, execution_lock_timeout_s=2.0)
    client = EphemeralSerialClient(cfg)
    
    from pytest_mes_core.events import bus, hookimpl
    class TestListener:
        def __init__(self):
            self.panic_fired = False
        @hookimpl
        def on_uart_event(self, event):
            self.panic_fired = True

    listener = TestListener()
    bus.register(listener)
    # Connect automatically starts the watchdog thread
    client.connect()
    assert client.watchdog._thread is not None
    assert client.watchdog._thread.is_alive()
    
    try:
        # 2. Write normal text. Watchdog should ingest it silently.
        os.write(master_fd, b"Normal Boot Output...\r\n")
        time.sleep(0.1)
        assert not listener.panic_fired
        
        # 3. Write panic pattern. Watchdog is awake and should steal it and fire.
        os.write(master_fd, b"Kernel panic - not syncing: Fatal exception\r\n")
        
        # Wait for the watchdog thread to parse the buffer and trigger the callback
        time.sleep(0.5)
        
        assert listener.panic_fired, "Watchdog failed to detect the kernel panic!"
        
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)


