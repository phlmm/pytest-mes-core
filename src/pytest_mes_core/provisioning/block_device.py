import anyio
import structlog
import os
import stat
import logging
import subprocess
from pathlib import Path
from pytest_mes_core.provisioning.base import BaseProvisioner, ProvisioningError, ImageVerificationError
from pytest_mes_core.utils.process import LiveProcess, ProcessTimeoutError, ProcessExecutionError
logger = structlog.get_logger('mes_core.provisioning.block_device')

class BmapBlockDeviceProvisioner(BaseProvisioner):
    """
    Flashes physical block devices connected to the Host PC (via USB-SD-Mux or direct USB).
    Strictly enforces OS-level safety checks to prevent catastrophic Host PC destruction.
    """

    def __init__(self, host_block_device: str, timeout_s: int=300):
        self.host_block_device = host_block_device
        self.timeout_s = timeout_s

    def _pre_flight_safety_check(self) -> None:
        """Mathematically verifies the target is a valid, unmounted block device.
        
        Prevents catastrophic Host OS destruction by ensuring the device is unmounted
        and is a true block device, not a regular file system path.

        Raises:
            ProvisioningError: If the device doesn't exist, is not a block device,
                or cannot be unmounted.
        """
        logger.debug('executing_pre_flight_safety_checks_on_host_block_device', host_block_device=self.host_block_device)
        if not os.path.exists(self.host_block_device):
            err_msg = f'Block device {self.host_block_device} does not exist. Is the SD Mux toggled to Host mode? Is the USB unplugged?'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        mode = os.stat(self.host_block_device).st_mode
        if not stat.S_ISBLK(mode):
            err_msg = f"SECURITY TRIP: '{self.host_block_device}' is NOT a block device! Aborting flash to prevent catastrophic Host OS destruction."
            logger.critical('=' * 60)
            logger.critical('fatal_err_msg', err_msg=err_msg)
            logger.critical('=' * 60)
            raise ProvisioningError(err_msg)
        try:
            with open('/proc/mounts', 'r') as f:
                mounts = f.read()
                if self.host_block_device in mounts:
                    logger.warning('host_block_device_is_currently_mounted_attempting_unmount', host_block_device=self.host_block_device)
                    umount_cmd = f'umount {self.host_block_device}*'
                    logger.debug('executing_umount_cmd', umount_cmd=umount_cmd)
                    try:
                        subprocess.run(umount_cmd, shell=True, stderr=subprocess.DEVNULL)
                    except KeyboardInterrupt:
                        raise ProvisioningError('Unmount operation interrupted by operator (Ctrl+C).')
                    with open('/proc/mounts', 'r') as f2:
                        if self.host_block_device in f2.read():
                            err_msg = f"Failed to unmount {self.host_block_device}. Device is busy (Check 'lsof')."
                            logger.critical('fatal_err_msg', err_msg=err_msg)
                            raise ProvisioningError(err_msg)
                else:
                    logger.debug('os_confirms_host_block_device_is_unmounted_and_free', host_block_device=self.host_block_device)
        except FileNotFoundError:
            logger.debug('[Provisioning] /proc/mounts not found. Assuming non-Linux Host OS.')

    def provision(self, image_path: Path) -> bool:
        """Flashes a raw block image to the physical media using bmaptool.

        Hardware Flow:
            Uses bmaptool to securely and rapidly flash an image to physical media.
            Enforces a final POSIX 'sync' to flush RAM caches to silicon.

        Args:
            image_path: The path to the raw firmware image.

        Raises:
            ProvisioningError: If the firmware image is missing, the execution fails,
                or the flash operation times out.
        """
        if not image_path.exists():
            err_msg = f'Firmware image missing at {image_path}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        self._pre_flight_safety_check()
        logger.info('initiating_bmaptool_flash_of_name_to_host_block_device', name=image_path.name, host_block_device=self.host_block_device)
        cmd = ['bmaptool', 'copy', str(image_path), self.host_block_device]
        try:
            process = LiveProcess(cmd, self.timeout_s, logger).execute()
            stdout_lower = process.stdout.lower()
            if process.returncode != 0:
                log_path = process.export_log(Path('/tmp/mes_artifacts'))
                logger.error('provisioning_bmaptool_failed_with_code_returncode_full_trace_saved_to_log_path', returncode=process.returncode, log_path=log_path)
                if 'no such file' in stdout_lower and '.bmap' in stdout_lower:
                    raise ProvisioningError('bmaptool requires a .bmap file next to the image, but it is missing.')
                elif 'permission denied' in stdout_lower:
                    raise ProvisioningError('Permission denied. Pytest must be run with sudo/root for block level access.')
                else:
                    raise ProvisioningError(f'bmaptool execution failed with code {process.returncode}.')
            try:
                logger.info('\n[Provisioning] Flash successful. Forcing kernel sync to flush RAM buffers to silicon...')
                subprocess.run(['sync'], check=True)
                logger.info('[Provisioning] Forcing kernel to rescan partition table geometry...')
                subprocess.run(['partprobe', self.host_block_device], check=False)
            except KeyboardInterrupt:
                raise ProvisioningError('Post-flash sync/partprobe interrupted by operator (Ctrl+C).')
            logger.info('image_successfully_provisioned_and_synced_in_duration_s_s', duration_s=process.duration_s)
        except ProcessTimeoutError:
            raise ProvisioningError('Block device flash timed out. Is the SD card physically defective?')
        except ProcessExecutionError as e:
            err_msg = f'Failed to execute OS command. Is bmaptool installed? Error: {e}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        return True
    async def async_provision(self, image_path: Path) -> bool:
        return await anyio.to_thread.run_sync(self.provision, image_path)
