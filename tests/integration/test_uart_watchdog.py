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
    
    # Track watchdog fires
    panic_fired = False
    def panic_callback():
        nonlocal panic_fired
        panic_fired = True

    client.watchdog.register_panic_callback(panic_callback)
    
    # Connect automatically starts the watchdog thread
    client.connect()
    assert client.watchdog._thread is not None
    assert client.watchdog._thread.is_alive()
    
    try:
        # 2. Write normal text. Watchdog should ingest it silently.
        os.write(master_fd, b"Normal Boot Output...\r\n")
        time.sleep(0.1)
        assert not panic_fired
        
        # 3. Request exclusive access. Watchdog should pause (but thread stays alive).
        with client.execution_lock():
            assert client._is_executing is True
            
            # Write a panic pattern while locked
            os.write(master_fd, b"Kernel panic - not syncing: Fatal exception\r\n")
            time.sleep(0.1)
            
            # The client should be able to read it because the watchdog is paused
            chunk = b""
            for _ in range(10):
                chunk = client.raw_read_chunk()
                if chunk:
                    break
                time.sleep(0.1)
            assert b"Kernel panic" in chunk
            assert not panic_fired
            
        # 4. Lock released. Watchdog thread restarts.
        assert client.watchdog._thread.is_alive()
        
        # 5. Write panic pattern. Watchdog is awake and should steal it and fire.
        os.write(master_fd, b"Kernel panic - not syncing: Fatal exception\r\n")
        
        # Wait for the watchdog thread to parse the buffer and trigger the callback
        time.sleep(0.5)
        
        assert panic_fired, "Watchdog failed to detect the kernel panic!"
        
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)

def test_uart_exclusive_raw_access_kills_watchdog():
    """
    Validates that the exclusive_raw_access context manager completely kills and joins
    the watchdog thread, allowing raw binary firmware flashing (like UUU) to safely 
    own the UART port without any byte stealing or thread concurrency issues.
    """
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    
    cfg = HostSerialConfig(port=slave_name, baudrate=115200, execution_lock_timeout_s=2.0)
    client = EphemeralSerialClient(cfg)
    client.connect()
    
    # 1. Thread should be alive
    original_thread = client.watchdog._thread
    assert original_thread is not None
    assert original_thread.is_alive()
    
    try:
        # 2. Enter exclusive mode
        with client.exclusive_raw_access() as raw_uart:
            # The client should be locked
            assert client._is_locked is True
            
            # The watchdog thread MUST be dead
            assert not original_thread.is_alive()
            
            # We should have access to the raw pyserial object
            import serial
            assert isinstance(raw_uart, serial.Serial)
            
            # We can write and read raw binary without the watchdog interfering
            os.write(master_fd, b"\\x00\\xFF\\x55\\xAARawBinaryData\\n")
            time.sleep(0.1)
            raw_bytes = raw_uart.read(raw_uart.in_waiting)
            assert b"RawBinaryData" in raw_bytes
            
        # 3. Exiting exclusive mode should respawn the watchdog
        assert client._is_locked is False
        new_thread = client.watchdog._thread
        assert new_thread is not None
        assert new_thread is not original_thread
        assert new_thread.is_alive()
        
    finally:
        client.disconnect()
        os.close(master_fd)
        os.close(slave_fd)
