# src/pytest_mes_core/host_adapters/diagnostics.py
import subprocess
import logging
from typing import Optional

logger = logging.getLogger("mes_core.host_adapters.diagnostics")

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
        logger.debug(f"[Diagnostics] Interrogating kernel for locks on {device_path}...")
        try:
            # 'fuser' returns the PIDs holding the file. stderr is redirected because fuser is noisy.
            logger.debug(f"[Diagnostics] Executing: fuser {device_path}")
            res_fuser = subprocess.run(
                ["fuser", device_path],
                capture_output=True, text=True, timeout=2.0
            )

            pids = res_fuser.stdout.strip().split()
            if not pids:
                logger.debug(f"[Diagnostics] Kernel reports {device_path} is completely free.")
                return None  # Device is free!

            logger.debug(f"[Diagnostics] fuser returned PIDs: {pids}")
            culprits = []
            for pid in pids:
                # Resolve the PID to an actual program name
                res_ps = subprocess.run(
                    ["ps", "-p", pid, "-o", "comm="],
                    capture_output=True, text=True, timeout=1.0
                )
                prog_name = res_ps.stdout.strip() or "unknown_process"
                culprits.append(f"PID {pid} ({prog_name})")

            result_str = ", ".join(culprits)
            logger.info(f"[Diagnostics] Hardware lock violation identified: {result_str}")
            return result_str

        except FileNotFoundError:
            # fuser is not installed. Fallback to lsof.
            logger.debug("[Diagnostics] 'fuser' not found. Falling back to 'lsof'...")
            try:
                res_lsof = subprocess.run(
                    ["lsof", "-t", device_path],
                    capture_output=True, text=True, timeout=2.0
                )
                pids = res_lsof.stdout.strip().split('\n')
                if pids and pids[0]:
                    result_str = f"PIDs: {', '.join(pids)} (Install 'psmisc' to see program names)"
                    logger.info(f"[Diagnostics] Hardware lock violation identified via lsof: {result_str}")
                    return result_str

                logger.debug(f"[Diagnostics] lsof reports {device_path} is completely free.")
                return None
            except FileNotFoundError:
                logger.warning("[Diagnostics] Neither 'fuser' nor 'lsof' is installed on Host PC. Cannot identify hardware lock owner.")

        except subprocess.TimeoutExpired:
            logger.warning(f"[Diagnostics] OS hung while checking owner of {device_path}. Zombie process?")

        return None
