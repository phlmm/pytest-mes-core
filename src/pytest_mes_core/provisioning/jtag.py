import anyio
import structlog
import time
import socket
import logging
from pathlib import Path
from pytest_mes_core.provisioning.base import BaseProvisioner, ProvisioningError, SiliconLockError, ImageVerificationError
logger = structlog.get_logger('mes_core.provisioning.jtag')

class OpenOcdRpcProvisioner(BaseProvisioner):
    """
    Connects to an active OpenOCD Daemon (managed by HostOpenOcdAdapter) via Telnet RPC.
    Executes raw silicon commands to halt, flash, verify, and reset ARM/RISC-V targets.
    """

    def __init__(self, rpc_port: int=4444, timeout_s: int=120):
        self.rpc_port = rpc_port
        self.timeout_s = timeout_s

    def provision(self, image_path: Path) -> bool:
        """Flashes the firmware via JTAG RPC using OpenOCD.

        Args:
            image_path: The absolute path to the firmware image to flash.

        Raises:
            ProvisioningError: If the firmware does not exist, connection is refused,
                or the OpenOCD daemon reports an error.
            SiliconLockError: If the target silicon is read/write protected.
            ImageVerificationError: If flash succeeds but verification fails.
        """
        if not image_path.exists():
            err_msg = f'Firmware image not found: {image_path}'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        logger.info('initiating_rpc_flash_of_name_on_port_rpc_port', name=image_path.name, rpc_port=self.rpc_port)
        safe_path = str(image_path.resolve()).replace('\\', '/')
        commands = ['reset halt', f'program {safe_path} verify reset', 'exit']
        full_output = ''
        t0 = time.perf_counter()
        try:
            logger.debug('connecting_to_openocd_daemon_at_127_0_0_1_rpc_port', rpc_port=self.rpc_port)
            with socket.create_connection(('127.0.0.1', self.rpc_port), timeout=self.timeout_s) as s:
                banner = s.recv(1024).decode('utf-8', errors='replace')
                logger.debug('connection_established_banner_val', val=banner.strip())
                for cmd in commands:
                    logger.debug('tx_cmd', cmd=cmd)
                    s.sendall(f'{cmd}\n'.encode('utf-8'))
                    cmd_output = ''
                    line_buffer = ''
                    while True:
                        try:
                            chunk = s.recv(4096).decode('utf-8', errors='replace')
                            if not chunk:
                                logger.debug('[JTAG-RPC] Socket closed by daemon.')
                                break
                            cmd_output += chunk
                            line_buffer += chunk
                            while '\n' in line_buffer:
                                line, line_buffer = line_buffer.split('\n', 1)
                                clean_line = line.strip('\r')
                                if clean_line and clean_line != '>':
                                    logger.debug('rx_clean_line', clean_line=clean_line)
                            if cmd_output.endswith('\n> ') or cmd_output.endswith('\r\n> '):
                                line_buffer = ''
                                break
                        except socket.timeout:
                            err_msg = f'JTAG RPC connection timed out after {self.timeout_s}s. Deadlocked SWD bus?'
                            logger.critical('fatal_err_msg', err_msg=err_msg)
                            logger.critical('output_before_timeout_cmd_output', cmd_output=cmd_output)
                            raise ProvisioningError(err_msg)
                    full_output += cmd_output
            self._evaluate_rpc_response(full_output)
            duration = round(time.perf_counter() - t0, 3)
            logger.info('firmware_successfully_flashed_and_verified_in_duration_s', duration=duration)
        except ConnectionRefusedError:
            err_msg = f'Connection refused on port {self.rpc_port}. Is the OpenOCD daemon running?'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise ProvisioningError(err_msg)
        return True

    async def async_provision(self, image_path, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.provision, image_path, *args, **kwargs)

    def _evaluate_rpc_response(self, stdout: str) -> None:
        """Parses the daemon's text stream to map cryptic C-errors to Domain Exceptions.

        Args:
            stdout: The complete textual output from the OpenOCD daemon.

        Raises:
            SiliconLockError: If the target silicon is read/write protected.
            ImageVerificationError: If flash succeeds but verification fails.
            ProvisioningError: If OpenOCD reports an error or lacks positive confirmation.
        """
        stdout_lower = stdout.lower()
        if 'locked' in stdout_lower or 'protection' in stdout_lower:
            logger.critical('fatal_silicon_rejected_flash_fuses_blown_log_stdout', stdout=stdout)
            raise SiliconLockError('Target silicon is read/write protected.')
        if 'verify failed' in stdout_lower or 'mismatch' in stdout_lower:
            logger.critical('fatal_flash_succeeded_but_verify_failed_bad_sector_on_chip_log_stdout', stdout=stdout)
            raise ImageVerificationError('JTAG readback verification failed.')
        if '** programming failed **' in stdout_lower or 'error:' in stdout_lower:
            logger.critical('fatal_openocd_reported_an_internal_error_stdout', stdout=stdout)
            raise ProvisioningError('OpenOCD reported a fatal error during the flash operation.')
        if 'wrote' not in stdout_lower and '** programming finished **' not in stdout_lower:
            logger.critical('fatal_missing_positive_confirmation_raw_output_stdout', stdout=stdout)
            raise ProvisioningError('OpenOCD completed without error, but did not confirm data was written.')