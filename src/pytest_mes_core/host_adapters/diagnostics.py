# src/pytest_mes_core/host_adapters/diagnostics.py
import subprocess
import logging
from typing import Optional

logger = logging.getLogger("mes_core.host_adapters.diagnostics")

class ResourceDiagnostics:
    """Interrogates the Linux kernel to find out who is hogging a hardware resource."""

    @staticmethod
    def get_device_owner(device_path: str) -> Optional[str]:
        """
        Checks if a /dev/ node is currently opened by any process on the OS.
        Returns a formatted string of the culprit (e.g., 'PID 4092 (minicom)'), or None if free.
        """
        try:
            # 'fuser' returns the PIDs holding the file. stderr is redirected because fuser is noisy.
            res_fuser = subprocess.run(
                ["fuser", device_path],
                capture_output=True, text=True, timeout=2.0
            )

            pids = res_fuser.stdout.strip().split()
            if not pids:
                return None  # Device is free!

            culprits = []
            for pid in pids:
                # Resolve the PID to an actual program name
                res_ps = subprocess.run(
                    ["ps", "-p", pid, "-o", "comm="],
                    capture_output=True, text=True, timeout=1.0
                )
                prog_name = res_ps.stdout.strip() or "unknown_process"
                culprits.append(f"PID {pid} ({prog_name})")

            return ", ".join(culprits)

        except FileNotFoundError:
            # fuser is not installed. Fallback to lsof.
            try:
                res_lsof = subprocess.run(
                    ["lsof", "-t", device_path],
                    capture_output=True, text=True, timeout=2.0
                )
                pids = res_lsof.stdout.strip().split('\n')
                if pids and pids[0]:
                    return f"PIDs: {', '.join(pids)} (Install 'psmisc' to see program names)"
            except FileNotFoundError:
                logger.warning("[Diagnostics] Neither 'fuser' nor 'lsof' is installed on Host PC.")

        except subprocess.TimeoutExpired:
            logger.warning(f"[Diagnostics] OS hung while checking owner of {device_path}")

        return None
