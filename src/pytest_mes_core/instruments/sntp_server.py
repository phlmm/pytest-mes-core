"""
pytest_mes_core.instruments.sntp_server
========================================

Controllable NTPv4 test server for firmware SNTP client validation.

Any MCU project whose firmware contains an SNTP client (NetX Duo, lwIP,
Zephyr, etc.) can reuse this instrument to:

- Provide deterministic time to the DUT during tests.
- Inject malformed responses for protocol robustness testing.
- Detect sync events via a UDP diagnostic log side-channel.

Usage::

    from pytest_mes_core.instruments.sntp_server import SntpServer

    with SntpServer(ntp_port=1234, log_port=8888, dut_ip="169.254.5.100") as srv:
        synced = srv.run(
            timeout_s=20.0,
            modify_response_fn=my_fn,   # optional
            wait_for_pattern="Synced from src",
        )

``modify_response_fn(response: bytearray, request: bytes) -> bytearray | None``
    Return the (optionally modified) response bytearray to send, or None to
    suppress sending a response entirely.

``wait_for_pattern``
    A substring that must appear in any UDP log line for ``run()`` to return
    True. If None, ``run()`` returns True as soon as the first NTP response
    is dispatched.
"""

from __future__ import annotations

import select
import socket
import struct
import threading
import time

import structlog
from collections.abc import Callable
from typing import Optional


logger = structlog.get_logger("mes_core.instruments.sntp_server")

ModifyFn = Callable[[bytearray, bytes], Optional[bytearray]]

NTP_EPOCH_OFFSET = 2_208_988_800  # seconds between 1900 and 1970


def build_ntp_response(request: bytes) -> bytearray:
    """Build a minimal valid NTPv4 server response for the given client request.

    This is exposed as a module-level function so consumer projects can
    build custom response payloads in their ``modify_response_fn`` callbacks
    without reimplementing the NTP packet format.
    """
    resp = bytearray(48)
    # Echo the client's VN, set mode=4 (server)
    client_vn = (request[0] & 0x38) if request else 0x18  # default VN=3
    resp[0] = client_vn | 0x04
    resp[1] = 1  # Stratum 1

    # Originate timestamp = client's transmit timestamp (bytes 40-47)
    if len(request) >= 48:
        resp[24:32] = request[40:48]

    ntp_now = int(time.time()) + NTP_EPOCH_OFFSET
    struct.pack_into(">I", resp, 32, ntp_now)      # receive timestamp
    struct.pack_into(">I", resp, 40, ntp_now + 1)  # transmit timestamp

    return resp


class SntpServer:
    """Context-manager NTPv4 test server for MCU SNTP client validation.

    Parameters
    ----------
    ntp_port:
        UDP port the DUT sends NTP requests to (firmware SNTP_SERVER_PORT).
    log_port:
        UDP port the DUT broadcasts diagnostic logs to.  Set to 0 to
        disable the log listener (if the DUT does not emit UDP logs).
    dut_ip:
        Expected source IP for NTP requests.  Currently informational
        only (all sources are served).
    """

    def __init__(
        self,
        ntp_port: int = 123,
        log_port: int = 0,
        dut_ip: str = "0.0.0.0",
    ) -> None:
        self.ntp_port = ntp_port
        self.log_port = log_port
        self.dut_ip = dut_ip
        self._ntp_sock: socket.socket | None = None
        self._log_sock: socket.socket | None = None
        self.last_request: bytes | None = None
        self.modify_response_fn: ModifyFn | None = None
        self.wait_for_pattern: str | None = None
        self.pattern_found_event = threading.Event()

    def __enter__(self) -> "SntpServer":
        self._ntp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ntp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._ntp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self._ntp_sock.bind(("", self.ntp_port))
        self._ntp_sock.setblocking(False)
        logger.debug("sntp_server_ntp_bound", port=self.ntp_port)

        if self.log_port > 0:
            self._log_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._log_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._log_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            self._log_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._log_sock.bind(("", self.log_port))
            self._log_sock.setblocking(False)
            logger.debug("sntp_server_log_bound", port=self.log_port)

        return self

    def __exit__(self, *_) -> None:
        if self._ntp_sock:
            self._ntp_sock.close()
            self._ntp_sock = None
        if self._log_sock:
            self._log_sock.close()
            self._log_sock = None

    def run(
        self,
        timeout_s: float = 20.0,
        modify_response_fn: Optional[ModifyFn] = None,
        wait_for_pattern: Optional[str] = None,
    ) -> bool:
        """Serve NTP requests until *wait_for_pattern* appears in a log line
        or *timeout_s* is exceeded.

        Returns
        -------
        bool
            True  -- *wait_for_pattern* was found (or first response sent
                     when *wait_for_pattern* is None)
            False -- timed out
        """
        assert self._ntp_sock, "Use as a context manager"

        socks: list[socket.socket] = [self._ntp_sock]
        if self._log_sock:
            socks.append(self._log_sock)

        deadline = time.monotonic() + timeout_s
        found = False

        mod_fn = modify_response_fn or self.modify_response_fn
        wait_pat = wait_for_pattern or self.wait_for_pattern

        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select(socks, [], [], min(1.0, remaining))

            if self._ntp_sock in ready:
                data, addr = self._ntp_sock.recvfrom(1024)
                logger.debug("sntp_request_received", src=addr[0], port=addr[1])
                self.last_request = data

                resp = build_ntp_response(data)
                if mod_fn:
                    result = mod_fn(resp, data)
                    if result is None:
                        continue  # caller suppressed the response
                    resp = result

                self._ntp_sock.sendto(resp, addr)

                if wait_pat is None:
                    return True  # success: responded to first request

            if self._log_sock and self._log_sock in ready:
                raw, _ = self._log_sock.recvfrom(2048)
                line = raw.decode("utf-8", errors="ignore").strip()
                if wait_pat and wait_pat in line:
                    self.pattern_found_event.set()
                    found = True
                    break

        return found
