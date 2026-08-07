from __future__ import annotations
"""
pytest_mes_core.mcu_log_parser
================================

Configurable log-to-event parser for MCU firmware diagnostics.

Different MCU projects emit diagnostic logs via different transports
(UART, UDP broadcast, SWD ITM, MQTT fields) and in different formats.
This module provides a transport-agnostic pattern matcher that converts
raw log lines into FSM triggers.

Usage::

    from pytest_mes_core.mcu_log_parser import McuLogParser, LogPattern

    parser = McuLogParser(patterns=[
        LogPattern(name="phy_link_up",   regex=r"PHY link UP",         trigger="network_up"),
        LogPattern(name="sntp_synced",   regex=r"Synced from src",     trigger="time_synced"),
        LogPattern(name="mqtt_connect",  regex=r"MQTT session active", trigger="mqtt_connected"),
        LogPattern(name="iwdg_reset",    regex=r"iwdg=(\\d+)",         trigger=None, capture="iwdg_count"),
    ])

    # Feed lines from any source (UDP, UART, SWD trace, etc.)
    for line in udp_socket_lines:
        events = parser.feed_line(line)
        for evt in events:
            app_fsm.feed_event(evt.trigger)

Patterns with ``trigger=None`` are capture-only: they extract named
groups into ``parser.captures`` without firing FSM transitions. This
is useful for forensic data (reset counters, PHY IDs, etc.) that tests
read after the boot sequence completes.
"""


import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern

import structlog

logger = structlog.get_logger("mes_core.mcu_log_parser")


@dataclass(frozen=True)
class LogPattern:
    """A single pattern to match against incoming log lines.

    Parameters
    ----------
    name:
        Human-readable identifier for this pattern (used in logs).
    regex:
        Regular expression to match. Named groups are extracted into
        ``MatchedEvent.groups``.
    trigger:
        FSM trigger to fire when the pattern matches. Set to None
        for capture-only patterns.
    capture:
        Optional key name. When set, the full match (or first group)
        is stored in ``McuLogParser.captures[capture]`` for later
        retrieval by tests.
    once:
        If True, the pattern fires only on the first match and is
        then disabled for the remainder of the session. Useful for
        boot-time events that should not re-trigger.
    """

    name: str
    regex: str
    trigger: Optional[str] = None
    capture: Optional[str] = None
    once: bool = False


@dataclass
class MatchedEvent:
    """Result of a successful pattern match."""

    pattern: LogPattern
    trigger: Optional[str]
    groups: Dict[str, str]
    raw_line: str


class McuLogParser:
    """Transport-agnostic log line pattern matcher.

    Parameters
    ----------
    patterns:
        List of ``LogPattern`` instances defining what to look for.
    """

    def __init__(self, patterns: List[LogPattern]) -> None:
        self._patterns: List[tuple[LogPattern, Pattern[str]]] = [
            (p, re.compile(p.regex)) for p in patterns
        ]
        self._fired_once: set[str] = set()
        self.captures: Dict[str, str] = {}
        self.match_count: Dict[str, int] = {p.name: 0 for p in patterns}

    def feed_line(self, line: str) -> List[MatchedEvent]:
        """Match a single log line against all active patterns.

        Returns a list of ``MatchedEvent`` objects for every pattern
        that matched. The list may be empty (no match) or contain
        multiple entries (a line can match multiple patterns).
        """
        events: List[MatchedEvent] = []

        for pat, compiled in self._patterns:
            # Skip patterns that already fired their once-only match
            if pat.once and pat.name in self._fired_once:
                continue

            m = compiled.search(line)
            if m is None:
                continue

            self.match_count[pat.name] += 1

            groups = m.groupdict()

            # Store capture value if configured
            if pat.capture:
                value = m.group(1) if m.lastindex else m.group(0)
                self.captures[pat.capture] = value
                logger.debug(
                    "log_capture",
                    pattern=pat.name,
                    key=pat.capture,
                    value=value,
                )

            if pat.once:
                self._fired_once.add(pat.name)

            evt = MatchedEvent(
                pattern=pat,
                trigger=pat.trigger,
                groups=groups,
                raw_line=line,
            )
            events.append(evt)

            if pat.trigger:
                logger.debug(
                    "log_pattern_matched",
                    pattern=pat.name,
                    trigger=pat.trigger,
                )

        return events

    def feed_lines(self, lines: List[str]) -> List[MatchedEvent]:
        """Convenience: feed multiple lines at once."""
        all_events: List[MatchedEvent] = []
        for line in lines:
            all_events.extend(self.feed_line(line))
        return all_events

    def reset(self) -> None:
        """Clear all state (captures, once-fired flags, match counts)."""
        self._fired_once.clear()
        self.captures.clear()
        self.match_count = {name: 0 for name in self.match_count}
