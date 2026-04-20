import pytest
import logging
import time
from pytest_mes_core.utils.daemon import DaemonProcess, DaemonStartupError

def test_daemon_process_starts_and_stops():
    logger = logging.getLogger("test")
    # Tail -f /dev/null is a classic way to keep a process alive
    daemon = DaemonProcess(cmd=["tail", "-f", "/dev/null"], logger=logger)
    
    daemon.start()
    assert daemon.proc is not None
    assert daemon.proc.poll() is None
    
    daemon.stop()
    assert daemon.proc is None

def test_daemon_ready_phrase():
    logger = logging.getLogger("test")
    # Echo immediately outputs the phrase, then sleeps
    cmd = ["sh", "-c", "echo 'Server is ready!'; sleep 5"]
    daemon = DaemonProcess(cmd=cmd, logger=logger, ready_phrase="server is ready")
    
    daemon.start(timeout_s=2.0)
    assert daemon.proc is not None
    daemon.stop()

def test_daemon_ready_phrase_timeout():
    logger = logging.getLogger("test")
    cmd = ["sleep", "5"]
    daemon = DaemonProcess(cmd=cmd, logger=logger, ready_phrase="never gonna happen")
    
    with pytest.raises(DaemonStartupError, match="Daemon timed out waiting for ready state"):
        daemon.start(timeout_s=0.2)

def test_daemon_instant_crash():
    logger = logging.getLogger("test")
    cmd = ["ls", "/path/that/does/not/exist"]
    daemon = DaemonProcess(cmd=cmd, logger=logger)
    
    with pytest.raises(DaemonStartupError, match="Daemon crashed instantly"):
        daemon.start()

def test_daemon_export_log(tmp_path):
    logger = logging.getLogger("test")
    cmd = ["echo", "daemon output"]
    daemon = DaemonProcess(cmd=cmd, logger=logger)
    
    # Needs to instantly crash or finish to test log
    with pytest.raises(DaemonStartupError):
        daemon.start()
        
    time.sleep(0.1) # Let IO consumer finish
    
    log_file = daemon.export_log(tmp_path)
    
    assert log_file.exists()
    assert "daemon output" in log_file.read_text()
