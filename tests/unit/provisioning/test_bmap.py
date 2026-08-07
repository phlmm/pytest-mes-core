from unittest.mock import patch
import pytest
from pathlib import Path
from pytest_mes_core.provisioning import BmapBlockDeviceProvisioner, ProvisioningError

@patch('subprocess.run')
@patch('os.stat')
@patch('os.path.exists')
def test_bmap_provisioner_catches_non_block_devices(mock_exists, mock_stat, mock_run):
    mock_exists.return_value = True
    mock_stat.return_value.st_mode = 33188
    provisioner = BmapBlockDeviceProvisioner(host_block_device='/dev/fake_mmc')
    with pytest.raises(ProvisioningError, match='NOT a block device'):
        provisioner.provision(Path('dummy.img'))
    mock_run.assert_not_called()