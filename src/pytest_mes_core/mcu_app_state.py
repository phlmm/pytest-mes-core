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
from typing import Any, Dict, List, Optional

import structlog
from transitions import Machine, MachineError

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

        # Collect base + extension states
        base_states = [s.value for s in McuAppState]
        all_states = base_states + self.extra_states()

        self.machine = Machine(
            model=self,
            states=all_states,
            initial=initial_state,
            send_event=True,
            auto_transitions=False,
            after_state_change="_record_timestamp",
        )

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
    # Waiting for states (sync + async)
    # ------------------------------------------------------------------

    def wait_for(self, target_state: str, timeout_s: float = 30.0) -> bool:
        """Block until the FSM reaches *target_state* or timeout.

        Intended for use with ``anyio.to_thread.run_sync``.
        """
        import threading

        if self.state == target_state:
            return True

        deadline = time.monotonic() + timeout_s
        event = threading.Event()

        # Patch a temporary callback to wake the waiter
        cb_name = f"_wait_cb_{id(event)}"

        def _on_enter(event_data: Any) -> None:
            if self.state == target_state:
                event.set()

        setattr(self, cb_name, _on_enter)
        self.machine.on_enter(target_state, cb_name)

        try:
            remaining = max(0.0, deadline - time.monotonic())
            return event.wait(timeout=remaining)
        finally:
            # Clean up the temporary callback
            try:
                self.machine.get_state(target_state).on_enter.callbacks.discard(cb_name)
            except Exception:
                pass
            try:
                delattr(self, cb_name)
            except Exception:
                pass

    async def async_wait_for(self, target_state: str, timeout_s: float = 30.0) -> bool:
        """Async variant of ``wait_for``."""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: self.wait_for(target_state, timeout_s)
        )

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
        self.logger.info(
            "state_transition",
            new_state=self.state,
            trigger=getattr(event, "event", None)
            if hasattr(event, "event")
            else str(event),
        )
