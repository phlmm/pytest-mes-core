import pytest
import logging
from pytest_mes_core.utils.process import LiveProcess, ProcessTimeoutError

def test_live_process_execution():
    logger = logging.getLogger("test")
    # A simple command that echoes "hello"
    proc = LiveProcess(cmd=["echo", "hello"], timeout_s=5.0, logger=logger)
    
    result = proc.execute()
    
    assert result.executed is True
    assert result.returncode == 0
    assert "hello\n" in result.stdout
    assert result.duration_s > 0

def test_live_process_already_executed():
    logger = logging.getLogger("test")
    proc = LiveProcess(cmd=["echo", "hello"], timeout_s=5.0, logger=logger)
    proc.execute()
    
    with pytest.raises(RuntimeError, match="has already been executed"):
        proc.execute()

def test_live_process_timeout():
    logger = logging.getLogger("test")
    # A command that sleeps for 5 seconds but timeout is 0.1
    proc = LiveProcess(cmd=["sleep", "5"], timeout_s=0.1, logger=logger)
    
    with pytest.raises(ProcessTimeoutError):
        proc.execute()
        
    assert proc.returncode == -1
    assert proc.executed is True

def test_live_process_export_log(tmp_path):
    logger = logging.getLogger("test")
    proc = LiveProcess(cmd=["echo", "export_test"], timeout_s=5.0, logger=logger)
    proc.execute()
    
    log_file = proc.export_log(tmp_path)
    
    assert log_file.exists()
    assert "export_test\n" in log_file.read_text()
