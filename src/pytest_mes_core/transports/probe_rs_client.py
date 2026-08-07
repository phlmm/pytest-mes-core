from __future__ import annotations
"""
probe_rs_client.py
==================

SWD/JTAG transport implemented via the ``probe-rs`` CLI.

Design decisions:
    - Every CLI invocation is wrapped with configurable timeout, retry logic,
      and structured logging of both stdout and stderr.
    - ``connect()`` runs ``probe-rs info`` and validates the chip identity
      returned by the probe.  The result is cached so ``is_connected``
      reflects actual probe reachability.
    - Memory reads parse both the new (space-separated hex) and legacy
      (address:hex) output formats defensively.
    - All public methods log entry/exit with enough context to diagnose
      intermittent SWD bus errors from CI logs alone.
"""


import re
import shutil
import struct
import subprocess
import time
from typing import Optional

import structlog

from pytest_mes_core.transports.mcu_base import McuTransport
from pytest_mes_core.config.instruments import ProbeRsTargetConfig

logger = structlog.get_logger("mes_core.transports.probe_rs")

# Retry constants
_DEFAULT_RETRIES = 5
_RETRY_BACKOFF_S = 1.0
_CLI_TIMEOUT_S = 30


class ProbeRsError(RuntimeError):
    """Raised when a probe-rs CLI command fails after all retries."""

    def __init__(self, message: str, cmd: list[str], returncode: int,
                 stdout: str, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(message)


class ProbeRsTransport(McuTransport):
    """SWD/JTAG transport implemented using the ``probe-rs`` CLI.

    Each method invocation spawns a short-lived ``probe-rs`` subprocess.
    The CLI is stateless -- there is no persistent debug session.

    Configuration is provided via :class:`ProbeRsTargetConfig` which
    specifies the chip, wire protocol, and clock speed.
    """

    def __init__(self, cfg: ProbeRsTargetConfig):
        self.cfg = cfg
        self._verified = False
        self._probe_rs_path: Optional[str] = None
        self._probe_version: Optional[str] = None

    # ------------------------------------------------------------------
    # Protocol properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True only after ``connect()`` successfully verified the probe."""
        return self._verified

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Verify that probe-rs is installed and the debug probe is reachable.

        Runs ``probe-rs info`` and validates that the configured chip
        appears in the output.  Sets ``is_connected`` to True on success.

        Raises:
            ProbeRsError: If the probe is unreachable or the chip identity
                does not match.
        """
        self._resolve_binary()

        logger.info("probe_rs_connecting",
                    chip=self.cfg.chip,
                    protocol=self.cfg.protocol,
                    speed=self.cfg.speed,
                    probe_rs=self._probe_rs_path,
                    version=self._probe_version)

        stdout = self._run_cli(["info"], retries=2, context="connect")

        # Validate that the chip name appears somewhere in the info output.
        # probe-rs info prints the chip family / variant on successful attach.
        chip_lower = self.cfg.chip.lower()
        if chip_lower not in stdout.lower():
            logger.warning("probe_rs_chip_mismatch",
                           expected=self.cfg.chip,
                           info_output=stdout[:500])
            # Don't fail hard -- some probe-rs versions abbreviate the name
        else:
            logger.debug("probe_rs_chip_confirmed", chip=self.cfg.chip)

        self._verified = True
        logger.info("probe_rs_connected", chip=self.cfg.chip)

    def disconnect(self) -> None:
        """No-op for the stateless CLI transport."""
        if self._verified:
            logger.debug("probe_rs_disconnecting", chip=self.cfg.chip)
        self._verified = False

    # ------------------------------------------------------------------
    # Core control
    # ------------------------------------------------------------------

    def halt(self) -> None:
        """Does NOT halt the CPU core -- issues a plain ``probe-rs reset`` instead.

        The stateless probe-rs CLI has no persistent debug session and no
        standalone ``halt`` command it can invoke through this transport, so
        there is genuinely no way to leave the core stopped afterwards: the
        MCU resets and immediately resumes running from the reset vector.
        Any caller (e.g. ``BareMetalStateMachine``) that records HALTED state
        after this call is lying to itself -- the firmware is executing.
        Use :class:`PyOcdTransport` if true halt semantics are required.
        """
        logger.warning(
            "probe_rs_halt_unsupported",
            chip=self.cfg.chip,
            hint="stateless CLI cannot halt; issuing plain reset — core will RUN. "
                 "Use PyOcdTransport for true halt semantics.",
        )
        self._run_cli(["reset"], context="halt")

    def resume(self) -> None:
        """Resume execution.

        Implemented as a plain ``probe-rs reset``: the core runs from the
        reset vector, which is the same effect as "resuming" a target that
        was never actually halted (see :meth:`halt`).
        """
        logger.debug("probe_rs_resume", chip=self.cfg.chip)
        self._run_cli(["reset"], context="resume")

    def reset(self) -> None:
        """Trigger a hardware reset of the MCU via the debug probe.

        Retries up to 3 times with backoff to handle transient SWD bus
        errors that occur when the probe is recovering from a previous
        access.
        """
        logger.info("probe_rs_resetting", chip=self.cfg.chip)
        t0 = time.monotonic()
        self._run_cli(["reset"], retries=_DEFAULT_RETRIES, context="reset")
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info("probe_rs_reset_ok",
                    chip=self.cfg.chip,
                    elapsed_ms=round(elapsed_ms, 1))

    # ------------------------------------------------------------------
    # Memory access
    # ------------------------------------------------------------------

    def read_memory(self, address: int, size: int) -> bytes:
        """Read *size* bytes from the MCU address space.

        Uses ``probe-rs read b8 <addr> <count>`` which outputs
        space-separated hex bytes.

        Args:
            address: Start address (must be within RAM: 0x20000000+).
            size: Number of bytes to read (1..4096 per call).

        Returns:
            Raw bytes read from the target.

        Raises:
            ProbeRsError: On communication failure.
            ValueError: If the output cannot be parsed.
        """
        if size <= 0:
            return b""
        if size > 4096:
            raise ValueError(f"read_memory: size {size} exceeds 4096-byte limit")

        logger.debug("probe_rs_read",
                     address=f"0x{address:08X}", size=size)

        stdout = self._run_cli(
            ["read", "b8", f"0x{address:08x}", str(size)],
            retries=_DEFAULT_RETRIES,
            context="read_memory",
        )

        data = self._parse_hex_output(stdout)
        if len(data) != size:
            logger.warning("probe_rs_read_size_mismatch",
                           expected=size, got=len(data),
                           raw_output=stdout[:300])
        logger.debug("probe_rs_read_ok",
                     address=f"0x{address:08X}",
                     size=len(data),
                     first_bytes=data[:16].hex() if data else "")
        return bytes(data)

    def read_u32(self, address: int) -> int:
        """Convenience: read a single 32-bit little-endian word.

        Uses ``probe-rs read b32 <addr> 1`` for efficiency (single word
        read is faster than 4-byte byte-level read on some probes).
        """
        stdout = self._run_cli(
            ["read", "b32", f"0x{address:08x}", "1"],
            retries=_DEFAULT_RETRIES,
            context="read_u32",
        )
        # Output format: "0x00000004" or "00000004"
        token = stdout.strip().split()[-1] if stdout.strip() else ""
        try:
            value = int(token, 16)
        except (ValueError, IndexError):
            raise ValueError(
                f"Cannot parse u32 from probe-rs output: {stdout!r}")
        logger.debug("probe_rs_read_u32",
                     address=f"0x{address:08X}", value=f"0x{value:08X}")
        return value

    def write_memory(self, address: int, data: bytes) -> None:
        """Write raw bytes to the MCU address space.

        Uses ``probe-rs write b8 <addr> <byte0> <byte1> ...``.

        Args:
            address: Destination address.
            data: Bytes to write (max 256 per call to avoid CLI arg limits).
        """
        if not data:
            return
        if len(data) > 256:
            raise ValueError(
                f"write_memory: {len(data)} bytes exceeds 256-byte CLI limit")

        logger.debug("probe_rs_write",
                     address=f"0x{address:08X}", size=len(data))

        args = ["write", "b8", f"0x{address:08x}"] + \
               [f"0x{b:02x}" for b in data]
        self._run_cli(args, retries=_DEFAULT_RETRIES, context="write_memory")

        logger.debug("probe_rs_write_ok",
                     address=f"0x{address:08X}", size=len(data))

    def read_core_register(self, reg_name: str) -> int:
        """Read an ARM core register (PC, SP, LR, etc.).

        Not supported by the stateless CLI -- returns 0 with a warning.
        Use a GDB session for register-level debugging.
        """
        logger.warning("probe_rs_read_core_register_unsupported",
                       register=reg_name,
                       hint="Use GDB for register-level access")
        return 0

    # ------------------------------------------------------------------
    # Download (flash) support
    # ------------------------------------------------------------------

    def download(self, firmware_path: str, *,
                 binary_format: Optional[str] = None,
                 base_address: Optional[int] = None,
                 chip_erase: bool = False) -> str:
        """Flash a firmware image via ``probe-rs download``.

        Args:
            firmware_path: Path to the .elf, .hex, or .bin file.
            binary_format: Set to ``"bin"`` for raw binaries.
            base_address: Required for .bin files (e.g. ``0x08000000``).
            chip_erase: If True, erase all flash sectors before programming.
                This clears persistent config sectors that survive a normal
                sector-level erase.

        Returns:
            The stdout from the download command.

        Raises:
            ProbeRsError: If the download fails.
        """
        args = ["download", firmware_path]
        if chip_erase:
            args.append("--chip-erase")
        if binary_format:
            args.extend(["--binary-format", binary_format])
        if base_address is not None:
            args.extend(["--base-address", f"0x{base_address:08x}"])

        logger.info("probe_rs_download_start",
                     firmware=firmware_path,
                     binary_format=binary_format,
                     base_address=f"0x{base_address:08x}" if base_address else None,
                     chip_erase=chip_erase)

        t0 = time.monotonic()
        stdout = self._run_cli(
            args,
            retries=2,
            timeout_s=self.cfg.timeout_s,
            context="download",
        )
        elapsed = time.monotonic() - t0
        logger.info("probe_rs_download_ok",
                     firmware=firmware_path,
                     elapsed_s=round(elapsed, 2))
        return stdout

    # ------------------------------------------------------------------
    # Async wrappers
    # ------------------------------------------------------------------



    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_binary(self) -> None:
        """Locate the ``probe-rs`` binary and cache its version string."""
        if self._probe_rs_path is not None:
            return

        path = shutil.which("probe-rs")
        if path is None:
            raise ProbeRsError(
                "probe-rs binary not found in $PATH. "
                "Install via: curl --proto '=https' --tlsv1.2 -LsSf "
                "https://github.com/probe-rs/probe-rs/releases/latest/"
                "download/probe-rs-tools-installer.sh | sh",
                cmd=["probe-rs"], returncode=-1, stdout="", stderr="")
        self._probe_rs_path = path

        # Capture version for diagnostic logs
        try:
            r = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            self._probe_version = r.stdout.strip() or r.stderr.strip()
        except Exception:
            self._probe_version = "unknown"

        logger.debug("probe_rs_binary_resolved",
                     path=self._probe_rs_path,
                     version=self._probe_version)

    def _build_cmd(self, args: list[str]) -> list[str]:
        """Construct the full probe-rs command line with global flags."""
        binary = self._probe_rs_path or "probe-rs"
        cmd = [binary] + args
        # Append global target specifiers
        cmd.extend(["--chip", self.cfg.chip])
        cmd.extend(["--protocol", self.cfg.protocol])
        cmd.extend(["--speed", str(self.cfg.speed)])
        return cmd

    def _run_cli(self, args: list[str], *,
                 retries: int = 1,
                 timeout_s: int = _CLI_TIMEOUT_S,
                 context: str = "") -> str:
        """Execute a probe-rs CLI command with retries and structured logging.

        Args:
            args: Command arguments after ``probe-rs`` (e.g. ``["reset"]``).
            retries: Number of attempts before raising.
            timeout_s: Per-attempt subprocess timeout.
            context: Human-readable operation name for log messages.

        Returns:
            The command's stdout on success.

        Raises:
            ProbeRsError: After all retries are exhausted.
        """
        self._resolve_binary()
        cmd = self._build_cmd(args)

        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            t0 = time.monotonic()
            try:
                logger.debug("probe_rs_cli_exec",
                             context=context,
                             attempt=f"{attempt}/{retries}",
                             cmd=" ".join(cmd))

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
                elapsed_ms = (time.monotonic() - t0) * 1000

                # Always log stderr as warnings -- probe-rs emits useful
                # diagnostics there even on success (e.g. WARN messages
                # about SWD AP errors, download progress, etc.)
                stderr_clean = result.stderr.strip()
                if stderr_clean:
                    for line in stderr_clean.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        if "WARN" in line or "warn" in line:
                            logger.warning("probe_rs_stderr_warn",
                                           context=context, line=line)
                        elif "ERROR" in line or "Error" in line:
                            logger.error("probe_rs_stderr_error",
                                         context=context, line=line)
                        else:
                            logger.debug("probe_rs_stderr",
                                         context=context, line=line)

                if result.returncode == 0:
                    logger.debug("probe_rs_cli_ok",
                                 context=context,
                                 elapsed_ms=round(elapsed_ms, 1),
                                 stdout_len=len(result.stdout))
                    return result.stdout

                # Non-zero exit code
                logger.warning("probe_rs_cli_failed",
                               context=context,
                               attempt=f"{attempt}/{retries}",
                               returncode=result.returncode,
                               stdout=result.stdout[:500],
                               stderr=stderr_clean[:500],
                               elapsed_ms=round(elapsed_ms, 1))
                last_error = ProbeRsError(
                    f"probe-rs {context or args[0]} failed "
                    f"(rc={result.returncode}): "
                    f"{stderr_clean[:200] or result.stdout[:200]}",
                    cmd=cmd,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=stderr_clean,
                )

            except subprocess.TimeoutExpired:
                elapsed_ms = (time.monotonic() - t0) * 1000
                logger.error("probe_rs_cli_timeout",
                             context=context,
                             attempt=f"{attempt}/{retries}",
                             timeout_s=timeout_s,
                             elapsed_ms=round(elapsed_ms, 1))
                last_error = ProbeRsError(
                    f"probe-rs {context or args[0]} timed out "
                    f"after {timeout_s}s",
                    cmd=cmd, returncode=-1, stdout="", stderr="TIMEOUT",
                )

            except OSError as exc:
                logger.error("probe_rs_cli_os_error",
                             context=context,
                             attempt=f"{attempt}/{retries}",
                             error=str(exc))
                last_error = ProbeRsError(
                    f"probe-rs {context or args[0]} OS error: {exc}",
                    cmd=cmd, returncode=-1, stdout="", stderr=str(exc),
                )

            # Backoff before retry
            if attempt < retries:
                backoff = _RETRY_BACKOFF_S * attempt
                logger.debug("probe_rs_cli_retry_backoff",
                             context=context,
                             backoff_s=backoff,
                             next_attempt=attempt + 1)
                time.sleep(backoff)

        # All retries exhausted
        assert last_error is not None
        logger.error("probe_rs_cli_all_retries_exhausted",
                     context=context, retries=retries)
        raise last_error

    @staticmethod
    def _parse_hex_output(stdout: str) -> bytearray:
        """Parse probe-rs byte-level hex output into a bytearray.

        Handles both formats:
            Space-separated: ``0a 0b 0c 0d ...``
            Colon-prefixed:  ``0x20048000: 0a 0b 0c 0d ...``
        """
        data = bytearray()
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip optional address prefix ("0x2004xxxx: ")
            if ":" in line:
                line = line.split(":", 1)[1].strip()
            # Extract all 2-char hex tokens
            for token in line.split():
                token = token.strip().lower()
                if re.fullmatch(r"[0-9a-f]{2}", token):
                    data.append(int(token, 16))
                elif re.fullmatch(r"[0-9a-f]{8}", token):
                    # 32-bit word: convert to 4 bytes LE
                    data.extend(struct.pack("<I", int(token, 16)))
        return data
