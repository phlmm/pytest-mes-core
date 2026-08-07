import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from pytest_mes_core.host_adapters.renode import RenodeRunner, RenodeRunnerError

def test_start_redirects_output_to_log_file_not_pipe(tmp_path):
    script = tmp_path / 'sim.resc'
    script.write_text('# fake renode script')
    runner = RenodeRunner(script, monitor_port=39999)
    fake_process = MagicMock()
    with patch('pytest_mes_core.host_adapters.renode.subprocess.Popen', return_value=fake_process) as mock_popen, patch.object(RenodeRunner, '_wait_for_port', return_value=True):
        runner.start()
    args, kwargs = mock_popen.call_args
    assert kwargs['stdout'] is not subprocess.PIPE
    assert hasattr(kwargs['stdout'], 'fileno')
    assert kwargs['stderr'] == subprocess.STDOUT
    log_file = runner._log_file
    assert log_file is not None
    assert not log_file.closed
    runner.stop()
    assert log_file.closed

def test_start_failure_includes_log_path_in_error(tmp_path):
    script = tmp_path / 'sim.resc'
    script.write_text('# fake renode script')
    runner = RenodeRunner(script, monitor_port=39998)
    fake_process = MagicMock()
    with patch('pytest_mes_core.host_adapters.renode.subprocess.Popen', return_value=fake_process), patch.object(RenodeRunner, '_wait_for_port', return_value=False):
        with pytest.raises(RenodeRunnerError, match='mes_renode_39998.log'):
            runner.start()
    assert runner._log_file is None or runner._log_file.closed