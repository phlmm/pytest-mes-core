import pluggy
from typing import Any
import structlog
from pydantic import BaseModel, ConfigDict
logger = structlog.get_logger("mes_core.events")

# ==========================================
# UART EVENT TYPES (Event-Driven Boot)
# ==========================================
class UartEvent(BaseModel):
    """Base class for all UART events produced during boot monitoring."""
    model_config = ConfigDict(frozen=True)
    elapsed_s: float

class PromptDetected(UartEvent):
    """A known prompt pattern was detected in the UART stream."""
    prompt_type: str  # "shell", "login", "password", "bootloader"

class AutobootWindowDetected(UartEvent):
    """The U-Boot autoboot countdown message was detected."""
    pass

class PanicDetected(UartEvent):
    """A kernel panic or secure boot violation was detected."""
    raw_output: str

class MilestoneReached(UartEvent):
    """A boot profiler milestone string was matched in the UART stream."""
    name: str

class BootDataReceived(UartEvent):
    """A complete line of boot output was received (for debug logging)."""
    line: str


hookspec = pluggy.HookspecMarker("mes_core")
hookimpl = pluggy.HookimplMarker("mes_core")

class EventBusSpecs:
    """Pluggy Hook Specifications for MES Core Event Bus."""
    
    @hookspec
    def on_uart_event(self, event: UartEvent) -> None:
        """Fired whenever a UART event (e.g., PromptDetected, PanicDetected) occurs."""

class EventBus:
    """
    Central event dispatcher for the MES framework.
    Allows components to communicate in a decoupled way using pluggy hooks and Pydantic events.
    """
    def __init__(self):
        self.pm = pluggy.PluginManager("mes_core")
        self.pm.add_hookspecs(EventBusSpecs)
        
    def register(self, plugin: Any) -> None:
        """Register a listener object containing @hookimpl methods."""
        self.pm.register(plugin)
        logger.debug("plugin_registered", plugin_name=getattr(plugin, "__name__", type(plugin).__name__))
        
    def emit_uart_event(self, event: UartEvent) -> None:
        """Dispatch a UART event to all registered listeners."""
        self.pm.hook.on_uart_event(event=event)

# Global Event Bus
bus = EventBus()
