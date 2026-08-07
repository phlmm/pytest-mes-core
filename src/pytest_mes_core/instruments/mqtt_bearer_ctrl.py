from __future__ import annotations
import functools
"""
pytest_mes_core.instruments.mqtt_bearer_ctrl
============================================

Controller for forcing/querying the MQTT transport bearer on a 0km R10 DUT
via the UART debug console.

The DUT firmware exposes two commands (handled by app_debug_cmd_thread.c):

  MQTT_BEARER_FORCE:<id>   Override bearer.  id: 0=auto 1=eth 2=wifi 3=lte
  MQTT_BEARER_QUERY        Print "active_bearer:<name> forced:<name>"

Usage in tests::

    def test_eth_bearer(mqtt_transport, mqtt_bearer_ctrl):
        mqtt_bearer_ctrl.force("eth")
        event = mqtt_transport.wait_for_event, "DEVICE_INFO", timeout_s=30
        assert event["telemetry"]["bearer"] == 1
"""


import re
import time
from functools import partial
from typing import Optional

import structlog

from pytest_mes_core.transports.serial_client import EphemeralSerialClient
from pytest_mes_core.transports.base import TransportTimeoutError

logger = structlog.get_logger("mes_core.instruments.mqtt_bearer_ctrl")

# Map human names → firmware bearer IDs (MqttBearer_t)
BEARER_IDS: dict[str, int] = {
    "auto": 0,
    "eth":  1,
    "wifi": 2,
    "lte":  3,
}
BEARER_NAMES: dict[int, str] = {v: k for k, v in BEARER_IDS.items()}

# Patterns emitted by the firmware after each command
_FORCE_ACK_PATTERN  = r"bearer_forced:"
_QUERY_ACK_PATTERN  = r"active_bearer:\s*\w+"
_ETH_DOWN_PATTERN   = r"ETH_LINK_DOWN"
_ETH_UP_PATTERN     = r"ETH_LINK_UP"


class MqttBearerController:
    """
    Controls the active MQTT bearer on the DUT via UART debug commands.

    All public methods have both synchronous and ``async_*`` variants.
    The async variants run the blocking UART I/O in a thread via anyio so
    they are safe to ``await`` inside pytest-anyio test coroutines.

    Parameters
    ----------
    uart:
        A connected ``EphemeralSerialClient`` instance (the ``uart_transport``
        fixture from conftest).
    default_reconnect_s:
        Time (seconds) to allow after forcing a bearer before checking that
        the device has reconnected.  Defaults to 45 s (ETH) or 120 s (LTE).
    """

    def __init__(
        self,
        uart: EphemeralSerialClient,
        default_reconnect_s: float = 45.0,
    ) -> None:
        self._uart = uart
        self.default_reconnect_s = default_reconnect_s

    # ------------------------------------------------------------------
    # Synchronous API
    # ------------------------------------------------------------------

    def force(self, bearer: str, ack_timeout_s: float = 5.0) -> None:
        """
        Send ``MQTT_BEARER_FORCE:<id>`` and wait for the firmware ACK.

        Parameters
        ----------
        bearer:
            One of "auto", "eth", "wifi", "lte".
        ack_timeout_s:
            Maximum time to wait for the ``bearer_forced:`` acknowledgement.

        Raises
        ------
        ValueError
            If *bearer* is not a recognised name.
        TransportTimeoutError
            If the firmware does not acknowledge within *ack_timeout_s*.
        """
        if bearer not in BEARER_IDS:
            raise ValueError(
                f"Unknown bearer {bearer!r}. Valid: {list(BEARER_IDS)}"
            )
        bid = BEARER_IDS[bearer]
        cmd = f"MQTT_BEARER_FORCE:{bid}"
        logger.debug("bearer_force_sending", cmd=cmd)
        self._uart.write_line(cmd)
        self._uart.expect(_FORCE_ACK_PATTERN, timeout_s=ack_timeout_s)
        logger.info("bearer_forced_ack", bearer=bearer, id=bid)

    def query(self, timeout_s: float = 5.0) -> tuple[str, str]:
        """
        Query the active and forced bearers.

        Returns
        -------
        (active_name, forced_name)
            Both are strings like "eth", "lte", "auto".

        Raises
        ------
        TransportTimeoutError
            If the firmware does not respond within *timeout_s*.
        """
        self._uart.write_line("MQTT_BEARER_QUERY")
        raw = self._uart.expect(_QUERY_ACK_PATTERN, timeout_s=timeout_s)
        active  = re.search(r"active_bearer:\s*(\w+)", raw)
        forced  = re.search(r"forced:\s*(\w+)", raw)
        active_name  = active.group(1)  if active  else "unknown"
        forced_name  = forced.group(1)  if forced  else "unknown"
        logger.debug("bearer_query_result", active=active_name, forced=forced_name)
        return active_name, forced_name

    def eth_link_down(self, timeout_s: float = 10.0) -> None:
        """Command the DUT to pull the PHY link down (for failover testing)."""
        self._uart.write_line("ETH_LINK_FORCE_DOWN")
        self._uart.expect(_ETH_DOWN_PATTERN, timeout_s=timeout_s)
        logger.info("eth_link_forced_down")

    def eth_link_up(self, timeout_s: float = 10.0) -> None:
        """Command the DUT to restore the PHY link."""
        self._uart.write_line("ETH_LINK_FORCE_UP")
        self._uart.expect(_ETH_UP_PATTERN, timeout_s=timeout_s)
        logger.info("eth_link_restored")

    # ------------------------------------------------------------------
    # Async API (anyio-compatible)
    # ------------------------------------------------------------------





    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def bearer_id(self, name: str) -> int:
        """Convert bearer name to firmware integer ID."""
        return BEARER_IDS.get(name, 0)

    def bearer_name(self, bid: int) -> str:
        """Convert firmware bearer integer to human name."""
        return BEARER_NAMES.get(bid, "unknown")
