"""
pytest_mes_core.mcu_app_state
==============================

Extensible application-level state machine for MCU firmware boot
sequences.

Sits **above** the hardware-level ``BareMetalStateMachine`` (which
tracks POWER_OFF/ENERGIZED/RUNNING) and models the firmware's own
initialisation stages that tests need to wait for before exercising
the DUT.

Design
------
The base class defines a minimal, universally-applicable boot chain::

    OFFLINE -> BOOTING -> NETWORK_UP -> TIME_SYNCED -> APP_READY

Consumer projects extend this by:

1. Adding project-specific states (e.g. ``MQTT_CONNECTED``,
   ``BEARER_SELECTED``) via ``extra_states()``.
2. Adding project-specific transitions via ``extra_transitions()``.
3. Overriding ``on_enter_<STATE>`` callbacks for side-effects.

Example (0km R10 project)::

    class R10AppStateMachine(McuAppStateMachine):

        def extra_states(self):
            return ["MQTT_CONNECTED", "BEARER_SELECTED"]

        def extra_transitions(self):
            return [
                {"trigger": "mqtt_connected", "source": "TIME_SYNCED",
                 "dest": "MQTT_CONNECTED"},
                {"trigger": "bearer_selected", "source": "MQTT_CONNECTED",
                 "dest": "BEARER_SELECTED"},
            ]

        def on_enter_MQTT_CONNECTED(self, event):
            self.logger.info("mqtt_session_established")

The FSM is **observation-driven**: test fixtures feed it events
(MQTT messages, UDP log lines, ARP replies) and it transitions
automatically.  Tests can then gate on a target state::

    await app_fsm.async_wait_for("APP_READY", timeout_s=30.0)
"""

from __future__ import annotations

import enum
import time
from typing import Any, Dict, List, Optional, Set
import threading

import structlog
from transitions import MachineError
from transitions.extensions.asyncio import AsyncMachine

logger = structlog.get_logger("mes_core.mcu_app_state")


class McuAppState(str, enum.Enum):
    """Base application-level states common to all MCU firmware."""

    OFFLINE     = "OFFLINE"      # Firmware not executing or not observable
    BOOTING     = "BOOTING"      # Reset vector reached, peripherals initialising
    NETWORK_UP  = "NETWORK_UP"   # IP stack has a routable address (ARP responds)
    TIME_SYNCED = "TIME_SYNCED"  # RTC synchronised via SNTP/NTP
    APP_READY   = "APP_READY"    # Application main loop entered, telemetry active


class McuAppStateMachine:
    """Extensible application-level FSM for MCU boot sequences.

    Parameters
    ----------
    initial_state:
        Starting state. Defaults to ``OFFLINE``.
    """

    def __init__(self, initial_state: str = McuAppState.OFFLINE.value) -> None:
        self.logger = logger.bind(fsm="app")
        self._state_timestamps: Dict[str, float] = {}
        self._async_waiters: Dict[str, Set[Any]] = {}

        # Collect base + extension states
        base_states = [s.value for s in McuAppState]
        all_states = base_states + self.extra_states()

        self.machine = AsyncMachine(
            model=self,
            states=all_states,
            initial=initial_state,
            send_event=True,
            auto_transitions=False,
            after_state_change="_record_timestamp",
        )
        for state in self.machine.states.values():
            state.on_enter.append('_notify_waiters')

        # Base transitions (linear boot chain)
        base_transitions = [
            {"trigger": "boot_started",  "source": "OFFLINE",     "dest": "BOOTING"},
            {"trigger": "network_up",    "source": "BOOTING",     "dest": "NETWORK_UP"},
            {"trigger": "time_synced",   "source": "NETWORK_UP",  "dest": "TIME_SYNCED"},
            {"trigger": "app_ready",     "source": "TIME_SYNCED", "dest": "APP_READY"},
            # Reset from any state
            {"trigger": "reset",         "source": "*",           "dest": "OFFLINE"},
            # Allow skipping intermediate states (fast boot)
            {"trigger": "app_ready",     "source": "NETWORK_UP",  "dest": "APP_READY"},
            {"trigger": "app_ready",     "source": "BOOTING",     "dest": "APP_READY"},
        ]

        for t in base_transitions + self.extra_transitions():
            self.machine.add_transition(**t)

    # ------------------------------------------------------------------
    # Extension points for consumer projects
    # ------------------------------------------------------------------

    def extra_states(self) -> List[str]:
        """Override to add project-specific states.

        Returns a list of state name strings that are appended to the
        base states.
        """
        return []

    def extra_transitions(self) -> List[Dict[str, Any]]:
        """Override to add project-specific transitions.

        Returns a list of dicts with keys: trigger, source, dest, and
        optionally conditions/after/before callbacks.
        """
        return []

    # ------------------------------------------------------------------
    # Event ingestion (call these from test fixtures)
    # ------------------------------------------------------------------

    def feed_event(self, trigger: str) -> bool:
        """Attempt to fire a trigger. Returns True if the transition
        was accepted, False if it was invalid for the current state.
        """
        try:
            self.trigger(trigger)
            return True
        except (MachineError, AttributeError):
            self.logger.debug(
                "transition_ignored",
                trigger=trigger,
                current_state=self.state,
            )
            return False

    # ------------------------------------------------------------------
    # Waiting for states (async only)
    # ------------------------------------------------------------------

    async def async_wait_for(self, target_state: str, timeout_s: float = 30.0) -> bool:
        """Async variant of ``wait_for``."""
        import anyio
        if self.state == target_state:
            return True

        event = anyio.Event()
        self._async_waiters.setdefault(target_state, set()).add(event)

        try:
            with anyio.fail_after(timeout_s):
                await event.wait()
            return True
        except TimeoutError:
            if target_state in self._async_waiters and event in self._async_waiters[target_state]:
                self._async_waiters[target_state].remove(event)
            return False

    async def _notify_waiters(self, event_data: Any) -> None:
        state = self.state
            
        if state in self._async_waiters:
            for ev in self._async_waiters[state]:
                ev.set()
            self._async_waiters[state].clear()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def state_durations(self) -> Dict[str, float]:
        """Time spent in each state since the FSM was created."""
        return dict(self._state_timestamps)

    def _record_timestamp(self, event: Any) -> None:
        """After-state-change callback: log the transition and record timing."""
        self._state_timestamps[self.state] = time.monotonic()
        trigger_name = getattr(event, "event", None) if hasattr(event, "event") else str(event)
        self.logger.info(
            "state_transition",
            new_state=self.state,
            trigger=trigger_name,
        )
        
        from pytest_mes_core.events import bus, StateChanged
        ev = StateChanged(
            fsm_name=self.__class__.__name__,
            old_state=event.transition.source,
            new_state=self.state,
            trigger=trigger_name or "unknown",
            timestamp=time.time()
        )
        bus.emit_state_event(event=ev)
