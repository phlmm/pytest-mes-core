import pytest
import logging
from pytest_mes_core.utils.process import LiveProcess, ProcessTimeoutError

def test_live_process_execution():
    logger = logging.getLogger('test')
    proc = LiveProcess(cmd=['echo', 'hello'], timeout_s=5.0, logger=logger)
    result = proc.execute()
    assert result.executed is True
    assert result.returncode == 0
    assert 'hello\n' in result.stdout
    assert result.duration_s > 0

def test_live_process_already_executed():
    logger = logging.getLogger('test')
    proc = LiveProcess(cmd=['echo', 'hello'], timeout_s=5.0, logger=logger)
    proc.execute()
    with pytest.raises(RuntimeError, match='has already been executed'):
        proc.execute()

def test_live_process_timeout():
    logger = logging.getLogger('test')
    proc = LiveProcess(cmd=['sleep', '5'], timeout_s=0.1, logger=logger)
    with pytest.raises(ProcessTimeoutError):
        proc.execute()
    assert proc.returncode == -1
    assert proc.executed is True

def test_live_process_export_log(tmp_path):
    logger = logging.getLogger('test')
    proc = LiveProcess(cmd=['echo', 'export_test'], timeout_s=5.0, logger=logger)
    proc.execute()
    log_file = proc.export_log(tmp_path)
    assert log_file.exists()
    assert 'export_test\n' in log_file.read_text()

def test_live_process_stdout_preserved_on_timeout():
    """Fix 7: stdout captured before the timeout must not be discarded --
    export_log() is used for forensics and must not write an empty trace."""
    logger = logging.getLogger('test')
    proc = LiveProcess(cmd=['bash', '-c', 'echo hello; sleep 5'], timeout_s=0.5, logger=logger)
    with pytest.raises(ProcessTimeoutError):
        proc.execute()
    assert proc.executed is True
    assert 'hello' in proc.stdout