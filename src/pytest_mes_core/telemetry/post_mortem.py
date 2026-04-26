import structlog
import time
import socket
import logging
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, Optional
logger = structlog.get_logger('mes_core.telemetry.post_mortem')

class JtagCrashDumper:
    """
    Connects to an active OpenOCD daemon upon test failure.
    Safely orchestrates hardware state extraction and Headless GDB unwinding
    without relying on hardcoded silicon magic numbers.
    """

    def __init__(self, rpc_port: int, gdb_port: int=3333, gdb_toolchain: str='gdb-multiarch'):
        self.rpc_port = rpc_port
        self.gdb_port = gdb_port
        self.gdb_toolchain = gdb_toolchain

    def _send_rpc(self, cmd: str, timeout_s: float=5.0) -> str:
        """Robust, EMI-resistant OpenOCD RPC client.

        Args:
            cmd: The OpenOCD command to send.
            timeout_s: The timeout for the socket operation.

        Returns:
            str: The output of the command.
        """
        logger.debug('rpc_tx_cmd', cmd=cmd)
        with socket.create_connection(('127.0.0.1', self.rpc_port), timeout=timeout_s) as s:
            s.recv(1024)
            s.sendall(f'{cmd}\n'.encode('utf-8'))
            output = ''
            while True:
                try:
                    chunk = s.recv(4096).decode('utf-8', errors='replace')
                    if not chunk:
                        break
                    output += chunk
                    if output.endswith('\n> ') or output.endswith('\r\n> '):
                        break
                except socket.timeout:
                    logger.critical('fatal_openocd_rpc_command_cmd_timed_out', cmd=cmd)
                    break
            clean_output = output.rsplit('\n> ', 1)[0].strip()
            logger.debug('rpc_rx_val_val_1', val=clean_output[:200], val_1='...' if len(clean_output) > 200 else '')
            return clean_output

    def execute_hardware_dump(self, dcc_addr: Optional[str]=None, stack_addr: Optional[str]=None) -> Dict[str, str]:
        """Extracts raw silicon state based strictly on configured addresses.

        Args:
            dcc_addr: Optional memory address of the DCC console.
            stack_addr: Optional memory address of the call stack.

        Returns:
            Dict[str, str]: The collected crash data containing registers,
                DCC console, stack memory, and any errors.
        """
        logger.critical('=' * 60)
        logger.critical('fatal_test_failure_detected_initiating_hardware_crash_dump')
        logger.critical('freezing_crime_scene_via_openocd_rpc_port_rpc_port', rpc_port=self.rpc_port)
        logger.critical('=' * 60)
        crash_data = {}
        try:
            self._send_rpc('halt')
            logger.debug('[Post-Mortem] Extracting CPU registers...')
            crash_data['registers'] = self._send_rpc('reg')
            if dcc_addr:
                logger.debug('extracting_arm_dcc_console_from_dcc_addr', dcc_addr=dcc_addr)
                crash_data['dcc_console'] = self._send_rpc(f'read_memory {dcc_addr} 32 100')
            if stack_addr:
                logger.debug('extracting_raw_stack_memory_from_stack_addr', stack_addr=stack_addr)
                crash_data['raw_stack'] = self._send_rpc(f'mdw {stack_addr} 64')
        except Exception as e:
            logger.critical('fatal_hardware_dump_failed_jtag_swd_connection_dropped_e', e=e)
            crash_data['error'] = str(e)
        return crash_data

    def execute_gdb_backtrace(self, elf_path: Path) -> str:
        """Uses headless GDB to generate a human-readable C-code backtrace.

        Uses thread-safe temporary files to prevent parallel worker collisions.

        Args:
            elf_path: Path to the ELF file.

        Returns:
            str: The backtrace output.
        """
        if not elf_path.exists():
            logger.warning('no_elf_file_found_at_elf_path_skipping_gdb_backtrace', elf_path=elf_path)
            return f'No ELF file found at {elf_path}.'
        logger.info('executing_headless_gdb_backtrace_on_port_gdb_port', gdb_port=self.gdb_port)
        gdb_commands = f'\n        target extended-remote localhost:{self.gdb_port}\n        set pagination off\n        echo \\n=== THREADS ===\\n\n        info threads\n        echo \\n=== BACKTRACE ===\\n\n        bt full\n        echo \\n=== LOCALS ===\\n\n        info locals\n        detach\n        quit\n        '
        with tempfile.NamedTemporaryFile(mode='w', suffix='.gdb', delete=True) as temp_script:
            temp_script.write(gdb_commands)
            temp_script.flush()
            cmd = [self.gdb_toolchain, '--batch', f'--command={temp_script.name}', str(elf_path.resolve())]
            try:
                logger.debug('spawning_local_host_pc_process_val', val=' '.join(cmd))
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15.0)
                if result.returncode != 0:
                    logger.error('gdb_unwinding_failed_code_returncode_val', returncode=result.returncode, val=result.stderr.strip())
                    return f'GDB Error (Code {result.returncode}): {result.stderr}'
                logger.info('[Post-Mortem] GDB Backtrace successfully generated.')
                return result.stdout
            except subprocess.TimeoutExpired:
                err_msg = 'GDB Unwind timed out. Target CPU might be entirely deadlocked or JTAG clock failed.'
                logger.critical('fatal_err_msg', err_msg=err_msg)
                return err_msg
            except FileNotFoundError:
                err_msg = f"GDB toolchain '{self.gdb_toolchain}' not found in system PATH."
                logger.critical('fatal_err_msg', err_msg=err_msg)
                return err_msg