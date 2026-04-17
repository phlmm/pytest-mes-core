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
            "i2cget -y 1 0x42": "0xABCD"
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
