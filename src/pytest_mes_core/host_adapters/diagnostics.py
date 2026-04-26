import structlog
import subprocess
import logging
from typing import Optional
logger = structlog.get_logger('mes_core.host_adapters.diagnostics')

class ResourceDiagnostics:
    """Interrogates the Linux kernel to find out who is hogging a hardware resource."""

    @staticmethod
    def get_device_owner(device_path: str) -> Optional[str]:
        """Checks if a /dev/ node is currently opened by any process on the OS.
        
        Uses fuser or lsof to identify process IDs and program names locking the device.

        Args:
            device_path: The absolute path to the hardware device (e.g. /dev/ttyUSB0).

        Returns:
            str: A formatted string of the culprit (e.g., 'PID 4092 (minicom)'), 
                or None if the device is free.
        """
        logger.debug('interrogating_kernel_for_locks_on_device_path', device_path=device_path)
        try:
            logger.debug('executing_fuser_device_path', device_path=device_path)
            res_fuser = subprocess.run(['fuser', device_path], capture_output=True, text=True, timeout=2.0)
            pids = res_fuser.stdout.strip().split()
            if not pids:
                logger.debug('kernel_reports_device_path_is_completely_free', device_path=device_path)
                return None
            logger.debug('fuser_returned_pids_pids', pids=pids)
            culprits = []
            for pid in pids:
                res_ps = subprocess.run(['ps', '-p', pid, '-o', 'comm='], capture_output=True, text=True, timeout=1.0)
                prog_name = res_ps.stdout.strip() or 'unknown_process'
                culprits.append(f'PID {pid} ({prog_name})')
            result_str = ', '.join(culprits)
            logger.info('hardware_lock_violation_identified_result_str', result_str=result_str)
            return result_str
        except FileNotFoundError:
            logger.debug("[Diagnostics] 'fuser' not found. Falling back to 'lsof'...")
            try:
                res_lsof = subprocess.run(['lsof', '-t', device_path], capture_output=True, text=True, timeout=2.0)
                pids = res_lsof.stdout.strip().split('\n')
                if pids and pids[0]:
                    result_str = f"PIDs: {', '.join(pids)} (Install 'psmisc' to see program names)"
                    logger.info('hardware_lock_violation_identified_via_lsof_result_str', result_str=result_str)
                    return result_str
                logger.debug('lsof_reports_device_path_is_completely_free', device_path=device_path)
                return None
            except FileNotFoundError:
                logger.warning("[Diagnostics] Neither 'fuser' nor 'lsof' is installed on Host PC. Cannot identify hardware lock owner.")
        except subprocess.TimeoutExpired:
            logger.warning('os_hung_while_checking_owner_of_device_path_zombie_process', device_path=device_path)
        return None