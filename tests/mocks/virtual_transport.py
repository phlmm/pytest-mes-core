# tests/mocks/virtual_transport.py
import re
from typing import Dict, Any, Union
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError

class MockTransport:
    """A deterministic, RAM-based Linux kernel simulator for Unit Testing."""
    def __init__(self, behavior_map: Dict[str, Union[CommandResult, Exception]]):
        self._connected = True
        self.behavior_map = behavior_map
        self.command_history = []

    @property
    def is_connected(self) -> bool: return self._connected

    def connect(self) -> None: self._connected = True
    def disconnect(self) -> None: self._connected = False

    def safe_run(self, cmd: str, timeout_s: float = 30.0, **kwargs: Any) -> CommandResult:
        self.command_history.append(cmd)
        if not self._connected:
            raise TransportConnectionError("Virtual Transport is disconnected.")

        for pattern, result in self.behavior_map.items():
            if re.search(pattern, cmd):
                # Simulate a catastrophic transport drop (e.g. Kernel Panic)
                if isinstance(result, Exception):
                    self._connected = False
                    raise result
                return result

        # Default fallback if the mock doesn't recognize the bash command
        return CommandResult(command=cmd, stdout="", stderr=f"sh: {cmd}: not found", exited=127, ok=False, duration_s=0.1)
