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

class IdleTick(UartEvent):
    """Emitted by UartEventStream after ~1s of RX silence so consumers can
    run timeout logic.  Purely local flow control -- never dispatched to the
    EventBus (it is not telemetry, just a heartbeat for the caller's own
    deadline checks when the UART itself is producing no events at all)."""
    pass

class StateChanged(BaseModel):
    """Fired when an FSM transitions from one state to another."""
    model_config = ConfigDict(frozen=True)
    fsm_name: str
    old_state: str
    new_state: str
    trigger: str
    timestamp: float


hookspec = pluggy.HookspecMarker("mes_core")
hookimpl = pluggy.HookimplMarker("mes_core")

class EventBusSpecs:
    """Pluggy Hook Specifications for MES Core Event Bus.

    Two layers of dispatch are provided:

    1. **Catch-all** — ``on_uart_event`` fires for every event.
       Use for generic loggers or telemetry sinks that aggregate all events.

    2. **Typed** — ``on_panic_detected``, ``on_boot_milestone``,
       ``on_prompt_detected`` fire only for their specific event type.
       Use when a plugin reacts to only one category (e.g. a Slack notifier
       that pings on panic, or a boot-time profiler tracking milestones).
    """

    @hookspec
    def on_uart_event(self, event: UartEvent) -> None:
        """Catch-all: fired for every UART event regardless of type."""

    @hookspec
    def on_panic_detected(self, event: PanicDetected) -> None:
        """Fired exclusively when a kernel panic or HAB/secure-boot violation
        is detected in the UART stream.  Suitable for alerting, failover
        triggers, or automated forensic capture hooks."""

    @hookspec
    def on_boot_milestone(self, event: MilestoneReached) -> None:
        """Fired when a boot-profiler milestone substring is matched.
        Suitable for SLA tracking, boot-time regression dashboards."""

    @hookspec
    def on_prompt_detected(self, event: PromptDetected) -> None:
        """Fired when a shell, bootloader, login, or password prompt is
        detected.  Suitable for session management hooks that need to react
        to console state changes without embedding logic inside the FSM."""

    @hookspec
    def on_state_changed(self, event: StateChanged) -> None:
        """Fired when an FSM transitions from one state to another."""


class EventBus:
    """
    Central event dispatcher for the MES framework.

    Components communicate in a decoupled way using pluggy hooks and Pydantic
    events.  The bus dispatches each event to **both** the catch-all hook and
    the appropriate typed hook so plugins can register at either granularity.
    """
    def __init__(self):
        self.pm = pluggy.PluginManager("mes_core")
        self.pm.add_hookspecs(EventBusSpecs)

    def register(self, plugin: Any) -> None:
        """Register a listener object containing @hookimpl methods."""
        self.pm.register(plugin)
        logger.debug("plugin_registered", plugin_name=getattr(plugin, "__name__", type(plugin).__name__))

    def emit_uart_event(self, event: UartEvent) -> None:
        """Dispatch a UART event to all registered listeners.

        Fires the catch-all ``on_uart_event`` hook for every event, then
        dispatches the appropriate typed hook based on the event's concrete
        type.  Both hooks are always called — order is catch-all first.
        """
        # 1. Catch-all (every plugin that implements on_uart_event)
        self.pm.hook.on_uart_event(event=event)

        # 2. Typed dispatch — only fires the hook matching the event type
        if isinstance(event, PanicDetected):
            self.pm.hook.on_panic_detected(event=event)
        elif isinstance(event, MilestoneReached):
            self.pm.hook.on_boot_milestone(event=event)
        elif isinstance(event, PromptDetected):
            self.pm.hook.on_prompt_detected(event=event)

    def emit_state_event(self, event: StateChanged) -> None:
        """Dispatch an FSM state change event to registered listeners."""
        self.pm.hook.on_state_changed(event=event)

# Global Event Bus singleton
bus = EventBus()

