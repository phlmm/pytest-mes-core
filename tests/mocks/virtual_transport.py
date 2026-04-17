# tests/mocks/virtual_transport.py
import re
from typing import Dict, Any, Union
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError

class MockTransport:
    """A deterministic, RAM-based Linux kernel simulator for Unit Testing."""
    def __init__(self, behavior_map: Dict[str, Union[CommandResult, Exception, str]] = None):
        self._connected = True
        self.behavior_map = behavior_map or {
            "cat /proc/device-tree/serial-number": "MOCK-TORADEX-9999",
            "swupdate -g": "testing\nswupdate\nB",
            "echo MES_HEARTBEAT": "MES_HEARTBEAT",
            "i2cget -y 1 0x42": "0xABCD",
            "cat /sys/class/thermal/thermal_zone0/temp": "45000",
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "1200000",
            "mmc extcsd read": "Device life time estimation type A [SEC_COUNT: 0x01]\nDevice life time estimation type B [SEC_COUNT: 0x02]\nPre EOL information [PRE_EOL_INFO: 0x01]",
            "test -e /sys/devices/system/edac/mc/mc0/ce_count": "",
            "cat /sys/devices/system/edac/mc/mc0/ce_count": "0",
            "cat /sys/devices/system/edac/mc/mc0/ue_count": "0",
            "memtester": "memtester version 4.5.1 (64-bit)\nDone.",
            "echo 0 > /sys/devices/system/edac": "",
            "systemd-analyze time": "Startup finished in 2.1s (kernel) + 4.5s (userspace) = 6.6s\ngraphical.target reached after 6.5s in userspace"
        }
        self.command_history = []

    @property
    def is_connected(self) -> bool: return self._connected

    def connect(self) -> None: self._connected = True
    def disconnect(self) -> None: self._connected = False

    def safe_run(
        self, 
        cmd: str, 
        timeout_s: float = 30.0, 
        check_exit_code: bool = False,
        auto_retry: bool = False,
        **kwargs: Any
    ) -> CommandResult:
        self.command_history.append(cmd)
        if not self._connected:
            raise TransportConnectionError("Virtual Transport is disconnected.")

        for pattern, result in self.behavior_map.items():
            if re.search(pattern, cmd) or pattern in cmd:
                # Simulate a catastrophic transport drop (e.g. Kernel Panic)
                if isinstance(result, Exception):
                    self._connected = False
                    raise result
                elif isinstance(result, str):
                    import time; time.sleep(0.05)
                    res = CommandResult(command=cmd, stdout=result, stderr="", exited=0, ok=True, duration_s=0.05)
                    if kwargs.get('check_exit_code') and not res.ok:
                        raise RuntimeError(f"Mock command failed: {cmd}")
                    return res
                return result

        # Default fallback if the mock doesn't recognize the bash command
        return CommandResult(command=cmd, stdout="", stderr=f"sh: {cmd}: not found", exited=127, ok=False, duration_s=0.1)

    def register_mock_response(self, command_substring: str, stdout: str):
        """Allows test projects to inject their own fake hardware responses."""
        self.behavior_map[command_substring] = stdout
