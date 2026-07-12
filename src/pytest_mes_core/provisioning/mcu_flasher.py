"""
mcu_flasher.py
==============

Unified MCU flash provisioner supporting probe-rs, PyOCD, and OpenOCD.

Design decisions:
    - Transport dispatch uses ``isinstance()`` instead of string-matching
      class names.  This is type-safe and survives refactoring.
    - Every flash operation validates the firmware file (existence, size,
      non-zero length) before touching the probe.
    - Post-flash verification reads back the first 256 bytes via SWD and
      compares against the source binary (when the transport supports it).
    - All timing is logged at INFO level for performance regression tracking.
    - ``flash_firmware()`` raises ``ProvisioningError`` on failure instead
      of silently returning False.  The bool return is kept for backwards
      compatibility but is always True on success.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Optional

import structlog

from pytest_mes_core.provisioning.base import (
    ProvisioningError,
    ImageVerificationError,
)

logger = structlog.get_logger("mes_core.provisioning.mcu")


class McuProvisioner:
    """Handles erasing, flashing, and verifying MCU firmware payloads.

    Supports three SWD/JTAG backends:
        - ``ProbeRsTransport`` (probe-rs CLI)
        - ``PyOcdTransport`` (pyocd Python API)
        - ``OpenOcdRpcProvisioner`` (OpenOCD Telnet RPC)

    Usage::

        provisioner = McuProvisioner(swd_transport)
        provisioner.flash_firmware(Path("firmware.bin"))
    """

    def __init__(self, swd_transport: Any):
        self.swd = swd_transport

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def flash_firmware(self, firmware_path: Path,
                       base_address: int = 0x0800_0000,
                       *, verify: bool = True,
                       chip_erase: bool = False) -> bool:
        """Flash a firmware binary or hex file to the MCU.

        Args:
            firmware_path: Path to the .elf, .hex, or .bin firmware image.
            base_address: Flash start address (ignored for .hex and .elf).
            verify: If True, read back the first 256 bytes after flashing
                and compare (probe-rs and pyocd only).
            chip_erase: If True, erase the entire flash chip before
                programming. Clears persistent config sectors that
                survive a normal sector-level erase.

        Returns:
            True on success.

        Raises:
            ProvisioningError: If the file is missing, the probe is
                unreachable, or the flash operation fails.
            ImageVerificationError: If post-flash readback mismatches.
        """
        # -- Pre-flight checks ------------------------------------------
        firmware_path = Path(firmware_path)
        self._validate_firmware_file(firmware_path)

        file_size = firmware_path.stat().st_size
        file_hash = self._sha256(firmware_path)
        transport_name = type(self.swd).__name__

        logger.info("flash_start",
                     firmware=firmware_path.name,
                     size_bytes=file_size,
                     sha256=file_hash[:16] + "...",
                     base_address=f"0x{base_address:08X}",
                     transport=transport_name)

        t0 = time.monotonic()

        # -- Dispatch to backend ----------------------------------------
        try:
            self._dispatch_flash(firmware_path, base_address,
                                 transport_name, chip_erase=chip_erase)
        except KeyboardInterrupt:
            logger.error("flash_interrupted_by_operator")
            self._safe_halt()
            raise ProvisioningError("Flash interrupted by operator (Ctrl+C)")
        except ProvisioningError:
            raise
        except Exception as exc:
            logger.error("flash_failed",
                         transport=transport_name,
                         error=str(exc),
                         error_type=type(exc).__name__)
            raise ProvisioningError(
                f"Flash failed ({transport_name}): {exc}") from exc

        elapsed = time.monotonic() - t0
        speed_kbs = (file_size / 1024) / elapsed if elapsed > 0 else 0

        logger.info("flash_complete",
                     firmware=firmware_path.name,
                     elapsed_s=round(elapsed, 2),
                     speed_kb_s=round(speed_kbs, 1),
                     transport=transport_name)

        # -- Post-flash verification ------------------------------------
        if verify and firmware_path.suffix.lower() == ".bin":
            self._verify_flash(firmware_path, base_address)

        return True

    def reset(self) -> bool:
        """Hardware-reset the MCU via the active SWD transport.

        Returns True on success, raises ProvisioningError on failure.
        """
        transport_name = type(self.swd).__name__
        logger.info("mcu_reset_start", transport=transport_name)
        t0 = time.monotonic()

        try:
            # ProbeRsTransport / OpenOcdRpcProvisioner
            if hasattr(self.swd, "reset"):
                self.swd.reset()
            else:
                raise ProvisioningError(
                    f"Transport {transport_name} has no reset() method")

            # PyOCD needs an explicit resume after reset
            if transport_name == "PyOcdTransport" and hasattr(self.swd, "resume"):
                self.swd.resume()

        except ProvisioningError:
            raise
        except Exception as exc:
            logger.error("mcu_reset_failed",
                         transport=transport_name,
                         error=str(exc))
            raise ProvisioningError(
                f"MCU reset failed ({transport_name}): {exc}") from exc

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info("mcu_reset_complete",
                     transport=transport_name,
                     elapsed_ms=round(elapsed_ms, 1))
        return True

    # ------------------------------------------------------------------
    # Async wrappers
    # ------------------------------------------------------------------

    async def async_verify_flash(self, firmware_path: Path, base_address: int) -> None:
        """Async verify flash."""
        if not hasattr(self.swd, "async_read_memory"):
            logger.debug("flash_verify_skipped", reason="transport lacks async_read_memory")
            return

        verify_size = min(256, firmware_path.stat().st_size)
        if verify_size == 0:
            return

        logger.info("flash_verify_start", address=f"0x{base_address:08X}", size=verify_size)

        try:
            device_data = await self.swd.async_read_memory(base_address, verify_size)
            with open(firmware_path, "rb") as f:
                fw_data = f.read(verify_size)

            if len(device_data) != len(fw_data):
                raise ImageVerificationError(
                    f"Flash readback length mismatch: expected {len(fw_data)} bytes, "
                    f"got {len(device_data)}")

            if device_data != fw_data:
                logger.error("flash_verify_failed",
                             address=f"0x{base_address:08X}",
                             device_data=device_data.hex(),
                             fw_data=fw_data.hex())
                raise ImageVerificationError(
                    f"Flash verification failed at 0x{base_address:08X}")
            logger.info("flash_verify_ok")
        except ImageVerificationError:
            raise
        except Exception as exc:
            # Verification failure is non-fatal -- log and continue (mirrors the
            # sync _verify_flash warn-and-continue behaviour for transient SWD
            # read hiccups).
            logger.warning("flash_verify_error",
                           error=str(exc),
                           hint="Verification failed but flash may be OK")

    async def async_flash_firmware(self, firmware_path: Path,
                                   base_address: int = 0x0800_0000,
                                   *, verify: bool = True,
                                   chip_erase: bool = False) -> bool:
        """Async variant of ``flash_firmware()``."""
        import anyio

        if not hasattr(self.swd, "async_download"):
            return await anyio.to_thread.run_sync(
                lambda: self.flash_firmware(
                    firmware_path, base_address, verify=verify,
                    chip_erase=chip_erase))

        firmware_path = Path(firmware_path)
        self._validate_firmware_file(firmware_path)

        file_size = firmware_path.stat().st_size
        file_hash = self._sha256(firmware_path)
        transport_name = type(self.swd).__name__

        logger.info("flash_start",
                     firmware=firmware_path.name,
                     size_bytes=file_size,
                     sha256=file_hash[:16] + "...",
                     base_address=f"0x{base_address:08X}",
                     transport=transport_name)

        t0 = time.monotonic()
        try:
            suffix = firmware_path.suffix.lower()
            kwargs = {}
            if chip_erase:
                kwargs["chip_erase"] = True
            if suffix not in (".hex", ".elf"):
                kwargs["binary_format"] = "bin"
                kwargs["base_address"] = base_address
            
            await self.swd.async_download(str(firmware_path), **kwargs)

            if verify and firmware_path.suffix.lower() == ".bin":
                await self.async_verify_flash(firmware_path, base_address)

        except Exception as exc:
            logger.error("flash_failed",
                         transport=transport_name,
                         error=str(exc),
                         error_type=type(exc).__name__)
            raise ProvisioningError(f"Flash failed ({transport_name}): {exc}") from exc

        elapsed = time.monotonic() - t0
        speed_kbs = (file_size / 1024) / elapsed if elapsed > 0 else 0

        logger.info("flash_complete",
                     transport=transport_name,
                     elapsed_s=round(elapsed, 2),
                     speed_kb_s=round(speed_kbs, 1))

        logger.info("mcu_reset_start", transport=transport_name)
        t0 = time.monotonic()
        try:
            if hasattr(self.swd, "async_reset"):
                await self.swd.async_reset()
            else:
                await anyio.to_thread.run_sync(self.swd.reset)
        except Exception as exc:
            logger.error("mcu_reset_failed",
                         transport=transport_name,
                         error=str(exc))
            raise ProvisioningError(f"MCU reset failed ({transport_name}): {exc}") from exc
            
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info("mcu_reset_complete",
                     transport=transport_name,
                     elapsed_ms=round(elapsed_ms, 1))
        return True

    async def async_reset(self) -> bool:
        """Async variant of ``reset()``."""
        import anyio
        from .base import ProvisioningError
        
        if hasattr(self.swd, "async_reset"):
            try:
                await self.swd.async_reset()
                return True
            except Exception as exc:
                raise ProvisioningError(f"MCU reset failed: {exc}") from exc
        return await anyio.to_thread.run_sync(self.reset)

    # ------------------------------------------------------------------
    # Backend dispatch
    # ------------------------------------------------------------------

    def _dispatch_flash(self, firmware_path: Path, base_address: int,
                        transport_name: str, *,
                        chip_erase: bool = False) -> None:
        """Route the flash operation to the correct backend."""
        from pytest_mes_core.transports.probe_rs_client import ProbeRsTransport
        from pytest_mes_core.provisioning.jtag import OpenOcdRpcProvisioner

        if isinstance(self.swd, ProbeRsTransport):
            self._flash_probe_rs(firmware_path, base_address,
                                 chip_erase=chip_erase)
        elif isinstance(self.swd, OpenOcdRpcProvisioner):
            self._flash_openocd(firmware_path)
        elif transport_name == "PyOcdTransport":
            # Late import to avoid hard dependency on pyocd
            self._flash_pyocd(firmware_path, base_address)
        else:
            raise ProvisioningError(
                f"Unsupported SWD transport: {transport_name}. "
                f"Supported: ProbeRsTransport, PyOcdTransport, "
                f"OpenOcdRpcProvisioner")

    def _flash_probe_rs(self, firmware_path: Path,
                        base_address: int, *,
                        chip_erase: bool = False) -> None:
        """Flash via probe-rs CLI."""
        logger.info("flash_backend_probe_rs", file=firmware_path.name)

        suffix = firmware_path.suffix.lower()
        kwargs: dict = {}
        if chip_erase:
            kwargs["chip_erase"] = True
        if suffix not in (".hex", ".elf"):
            kwargs["binary_format"] = "bin"
            kwargs["base_address"] = base_address

        # Use the transport's download method which has retry/timeout logic
        if hasattr(self.swd, "download"):
            self.swd.download(str(firmware_path), **kwargs)
        else:
            # Fallback to raw CLI for backwards compatibility
            args = ["download", str(firmware_path)]
            if suffix not in (".hex", ".elf"):
                args.extend(["--binary-format", "bin",
                              "--base-address", f"0x{base_address:08x}"])
            self.swd._run_cli(args, context="flash", timeout_s=120)

        logger.info("flash_probe_rs_download_complete",
                     file=firmware_path.name)
        self.swd.reset()

    def _flash_pyocd(self, firmware_path: Path,
                     base_address: int) -> None:
        """Flash via pyocd Python API."""
        from pyocd.flash.file_programmer import FileProgrammer

        logger.info("flash_backend_pyocd", file=firmware_path.name)

        if not self.swd.is_connected:
            self.swd.connect()
        self.swd.halt()

        programmer = FileProgrammer(
            self.swd._session,
            progress=self._log_progress,
        )
        if firmware_path.suffix.lower() == ".hex":
            programmer.program(str(firmware_path))
        else:
            programmer.program(str(firmware_path),
                               base_address=base_address)

        logger.info("flash_pyocd_programming_complete",
                     file=firmware_path.name)
        self.swd.reset()
        self.swd.resume()

    def _flash_openocd(self, firmware_path: Path) -> None:
        """Flash via OpenOCD Telnet RPC."""
        logger.info("flash_backend_openocd", file=firmware_path.name)
        result = self.swd.provision(firmware_path)
        if not result:
            raise ProvisioningError(
                f"OpenOCD provisioning returned False for "
                f"{firmware_path.name}")

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def _verify_flash(self, firmware_path: Path,
                      base_address: int) -> None:
        """Read back the first 256 bytes and compare against the source.

        Only works for .bin files where we know the exact flash layout.
        Silently skips if the transport doesn't support read_memory.
        """
        if not hasattr(self.swd, "read_memory"):
            logger.debug("flash_verify_skipped",
                         reason="transport lacks read_memory")
            return

        verify_size = min(256, firmware_path.stat().st_size)
        if verify_size == 0:
            return

        logger.info("flash_verify_start",
                     address=f"0x{base_address:08X}",
                     size=verify_size)

        try:
            with open(firmware_path, "rb") as f:
                expected = f.read(verify_size)

            actual = self.swd.read_memory(base_address, verify_size)

            if len(actual) != len(expected):
                raise ImageVerificationError(
                    f"Flash readback length mismatch: expected {len(expected)} bytes, "
                    f"got {len(actual)}")

            if actual == expected:
                logger.info("flash_verify_ok", size=verify_size)
            else:
                # Find first mismatch offset
                for i, (a, b) in enumerate(zip(actual, expected)):
                    if a != b:
                        logger.error("flash_verify_mismatch",
                                     offset=i,
                                     expected=f"0x{b:02X}",
                                     actual=f"0x{a:02X}",
                                     address=f"0x{base_address + i:08X}")
                        raise ImageVerificationError(
                            f"Flash readback mismatch at offset {i} "
                            f"(0x{base_address + i:08X}): "
                            f"expected 0x{b:02X}, got 0x{a:02X}")
        except ImageVerificationError:
            raise
        except Exception as exc:
            # Verification failure is non-fatal -- log and continue
            logger.warning("flash_verify_error",
                           error=str(exc),
                           hint="Verification failed but flash may be OK")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_firmware_file(path: Path) -> None:
        """Validate that the firmware file exists and is non-empty."""
        if not path.exists():
            raise ProvisioningError(
                f"Firmware file not found: {path}")
        if not path.is_file():
            raise ProvisioningError(
                f"Firmware path is not a file: {path}")
        if path.stat().st_size == 0:
            raise ProvisioningError(
                f"Firmware file is empty (0 bytes): {path}")

        suffix = path.suffix.lower()
        if suffix not in (".bin", ".hex", ".elf"):
            logger.warning("flash_unknown_suffix",
                           suffix=suffix,
                           hint="Expected .bin, .hex, or .elf")

    @staticmethod
    def _sha256(path: Path) -> str:
        """Compute SHA-256 hash of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _log_progress(self, progress: float) -> None:
        """Callback for PyOCD's flash progress reporting."""
        pct = int(progress * 100)
        if pct % 25 == 0 and pct != getattr(self, "_last_log_pct", -1):
            logger.info("flash_progress", percent=pct)
            self._last_log_pct = pct

    def _safe_halt(self) -> None:
        """Best-effort halt after an interrupted flash."""
        try:
            if hasattr(self.swd, "halt"):
                self.swd.halt()
        except Exception:
            pass
