import time
import os
import queue
import socket
import re
import logging
import functools
import anyio
from abc import ABC, abstractmethod
from typing import Dict, Optional, Pattern, Any, Callable, List, Generator
from dataclasses import dataclass, field
from transitions import EventData
from enum import Enum, auto
from tenacity import retry, stop_after_attempt, wait_fixed, before_sleep_log
from pytest_mes_core.transports.constants import ANSI_ESCAPE_B, PANIC_PATTERN_B

from pytest_mes_core.config import StateMachineConfig, BootProfilerConfig
from pytest_mes_core.instruments import ScpiPowerSupply
from pytest_mes_core.transports import EphemeralSerialClient, EphemeralSSHClient
from pytest_mes_core.transports import TransportTimeoutError, TransportConnectionError, TransportError
from pytest_mes_core.manifest import HardwareManifest

try:
    from transitions.extensions.asyncio import AsyncGraphMachine as Machine
    HAS_GRAPHVIZ = True
except ImportError:
    from transitions.extensions.asyncio import AsyncMachine as Machine
    HAS_GRAPHVIZ = False

def _is_headless() -> bool:
    """Check if we are running in a CI/CD headless environment."""
    return os.environ.get("CI", "").lower() in ("true", "1") or os.environ.get("MES_HEADLESS", "").lower() in ("true", "1")

import structlog

logger = structlog.get_logger("mes_core.state_machine")

# ==========================================
# DOMAIN EXCEPTIONS (State Machine Faults)
# ==========================================
class StateMachineError(Exception):
    """Root exception for all FSM hardware lifecycle failures."""
    pass

class KernelPanicError(StateMachineError):
    """Raised when the PANIC_WATCHDOG regex matches kernel output during boot."""
    pass

class BootloaderTimeoutError(StateMachineError):
    """Raised when the FSM cannot intercept the U-Boot prompt within the configured timeout."""
    pass

class BootloaderSyncError(StateMachineError):
    """Raised when the echo sync command fails after prompt detection."""
    pass

from pytest_mes_core.events import (
    UartEvent, PromptDetected, AutobootWindowDetected,
    PanicDetected, MilestoneReached, BootDataReceived, IdleTick, bus
)


class UartEventStream:
    """
    Synchronous generator-based UART event source.

    Replaces raw busy-wait loops with a typed event stream. The caller
    iterates over events and reacts to each one, keeping boot sequence
    logic clean and testable.

    Usage:
        stream = UartEventStream(serial, ANSI_ESCAPE_B, PANIC_WATCHDOG)
        for event in stream.open(prompts={"shell": b"root@"}, timeout_s=60):
            if isinstance(event, PromptDetected) and event.prompt_type == "shell":
                break
            elif isinstance(event, PanicDetected):
                raise KernelPanicError(event.raw_output)
    """

    def __init__(
        self,
        serial: 'EphemeralSerialClient',
        ansi_pattern: Pattern[bytes],
        panic_pattern: Pattern[bytes],
    ):
        """Initializes the event stream wrapper over the UART.

        Args:
            serial: The active Serial transport client.
            ansi_pattern: Regex pattern to strip ANSI terminal codes.
            panic_pattern: Regex pattern to detect fatal kernel crashes.
        """
        self.serial = serial
        self.ansi_pattern = ansi_pattern
        self.panic_pattern = panic_pattern

    def open(
        self,
        prompts: Dict[str, bytes],
        timeout_s: float = 60.0,
        milestones: Optional[Dict[str, str]] = None,
        autoboot_trigger: Optional[bytes] = None,
        flush: bool = True,
        active_ping_char: Optional[bytes] = None,
    ) -> Generator[UartEvent, None, None]:
        """
        Opens the UART event stream via the pub/sub subscriber queue and yields
        typed events.  Subscribing to the queue (rather than calling raw_read_chunk)
        enables true UART multiplexing: the watchdog, state machine, and any other
        consumer all receive every byte independently.

        Each prompt type is yielded at most once (dedup via ``detected_prompts``).
        Milestones are yielded once and removed from the pending set.
        PanicDetected terminates the generator and always dispatches to the EventBus.

        IMPORTANT — private local buffer
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        _rx_daemon_loop is the *sole* writer to self.serial.parser.  open() must
        never call parser.ingest() on the same chunk: that would double every byte
        in the shared buffer, producing garbled, interleaved lines at any chunk
        boundary.  Instead we maintain a private _local_buf here that is completely
        independent of all other concurrent parser consumers (expect, safe_run, the
        UartKernelWatchdog, etc.).
        """
        from pytest_mes_core.events import bus
        t_start = time.perf_counter()
        pending_milestones = dict(milestones) if milestones else {}
        autoboot_fired = False
        detected_prompts: set = set()

        if flush:
            self.serial.flush_buffers()

        rx_queue = self.serial.subscribe(maxsize=0)
        last_rx_time = time.perf_counter()
        last_event_yield = time.perf_counter()
        _local_buf = ""  # private — never shared with self.serial.parser
        _silent_pings = 0  # consecutive pings with no RX — TX health indicator

        try:
            while time.perf_counter() - t_start < timeout_s:
                try:
                    chunk = rx_queue.get(timeout=0.05)
                except queue.Empty:
                    # Raise silence threshold to 5 s: the Toradex module-loader
                    # genuinely pauses 3+ s between printk bursts.  A 2-s ping
                    # injects \n into the kernel console, echoing back noise that
                    # interleaves with kernel messages.
                    if active_ping_char and (time.perf_counter() - last_rx_time > 5.0):
                        _silent_pings += 1
                        if _silent_pings >= 6:
                            logger.error(
                                "[UART] TX health suspect: %d pings unanswered "
                                "(%.0f s silence). Check host→DUT UART TX wiring, "
                                "serial adapter, and connector pin assignment.",
                                _silent_pings, _silent_pings * 5.0,
                            )
                        elif _silent_pings >= 3:
                            logger.warning(
                                "[UART] %d consecutive pings unanswered (%.0f s) "
                                "— possible TX line fault.",
                                _silent_pings, _silent_pings * 5.0,
                            )
                        else:
                            logger.debug("[UART] Console silent. Injecting ping to redraw prompt...")
                        self.serial.raw_write(active_ping_char)
                        last_rx_time = time.perf_counter()
                    # Heartbeat: a silent UART produces no events at all, which
                    # starves any consumer that only checks its own deadlines
                    # "on every event loop iteration".  Yield a local-only
                    # IdleTick roughly every second so those checks still run.
                    if time.perf_counter() - last_event_yield >= 1.0:
                        elapsed = round(time.perf_counter() - t_start, 3)
                        last_event_yield = time.perf_counter()
                        yield IdleTick(elapsed_s=elapsed)
                    continue

                if not chunk:
                    continue

                if _silent_pings > 0:
                    logger.debug("[UART] RX resumed after %d silent pings.", _silent_pings)
                    _silent_pings = 0
                last_rx_time = time.perf_counter()

                # Decode into the private local buffer; strip ANSI escape codes.
                _local_buf += self.ansi_pattern.sub(b'', chunk).decode('utf-8', errors='replace')
                elapsed = round(time.perf_counter() - t_start, 3)

                # Encode current buffer state for byte-pattern matching.
                clean = _local_buf.encode('utf-8')

                # 1. Panic detection (always fatal — terminates the stream)
                if self.panic_pattern.search(clean):
                    ev = PanicDetected(
                        elapsed_s=elapsed,
                        raw_output=_local_buf[-500:],
                    )
                    bus.emit_uart_event(ev)
                    last_event_yield = time.perf_counter()
                    yield ev
                    return

                # 2. Autoboot window (once)
                if autoboot_trigger and not autoboot_fired and autoboot_trigger in clean:
                    autoboot_fired = True
                    ev = AutobootWindowDetected(elapsed_s=elapsed)
                    bus.emit_uart_event(ev)
                    last_event_yield = time.perf_counter()
                    yield ev

                # 3. Prompt detection — each type yielded at most once
                for ptype, pbytes in prompts.items():
                    if ptype not in detected_prompts and pbytes in clean:
                        detected_prompts.add(ptype)
                        logger.debug(f"[UART-FSM] Detected prompt '{ptype}' in buffer")
                        ev = PromptDetected(elapsed_s=elapsed, prompt_type=ptype)
                        bus.emit_uart_event(ev)
                        last_event_yield = time.perf_counter()
                        yield ev

                # 4. Line-level processing — extract complete lines from the
                # local buffer; leave any incomplete trailing fragment for the
                # next chunk.
                _local_buf = _local_buf.replace('\r\n', '\n').replace('\r', '\n')
                while '\n' in _local_buf:
                    line, _local_buf = _local_buf.split('\n', 1)
                    clean_line = line.strip()
                    if not clean_line:
                        continue
                    ev = BootDataReceived(elapsed_s=elapsed, line=clean_line)
                    bus.emit_uart_event(ev)
                    last_event_yield = time.perf_counter()
                    yield ev
                    found_keys = [k for k, v in pending_milestones.items() if v in clean_line]
                    for k in found_keys:
                        pending_milestones.pop(k)
                        ev2 = MilestoneReached(elapsed_s=elapsed, name=k)
                        bus.emit_uart_event(ev2)
                        last_event_yield = time.perf_counter()
                        yield ev2
        finally:
            self.serial.unsubscribe(rx_queue)

    async def open_async(
        self,
        prompts: Dict[str, bytes],
        timeout_s: float = 60.0,
        milestones: Optional[Dict[str, str]] = None,
        autoboot_trigger: Optional[bytes] = None,
        flush: bool = True,
        active_ping_char: Optional[bytes] = None,
    ) -> 'AsyncGenerator[UartEvent, None]':
        """
        Async variant of the UART event stream.

        Reads from the pub/sub subscriber queue via anyio offload so the event
        loop is never blocked.  Prompt deduplication and EventBus dispatch mirror
        the sync open() implementation.

        Uses the same private-local-buffer strategy as open() — never touches
        self.serial.parser to avoid the double-ingest race.
        """
        from pytest_mes_core.events import bus
        t_start = time.perf_counter()
        pending_milestones = dict(milestones) if milestones else {}
        autoboot_fired = False
        detected_prompts: set = set()

        if flush:
            self.serial.flush_buffers()

        rx_queue = self.serial.subscribe(maxsize=0)
        last_rx_time = time.perf_counter()
        last_event_yield = time.perf_counter()
        _local_buf = ""  # private — never shared with self.serial.parser
        _silent_pings = 0  # consecutive pings with no RX — TX health indicator

        try:
            while time.perf_counter() - t_start < timeout_s:
                # Offload blocking queue.get to thread so the event loop stays free
                try:
                    chunk = await anyio.to_thread.run_sync(
                        functools.partial(rx_queue.get, timeout=0.25)
                    )
                except queue.Empty:
                    if active_ping_char and (time.perf_counter() - last_rx_time > 5.0):
                        _silent_pings += 1
                        if _silent_pings >= 6:
                            logger.error(
                                "[UART] TX health suspect: %d pings unanswered "
                                "(%.0f s silence). Check host→DUT UART TX wiring, "
                                "serial adapter, and connector pin assignment.",
                                _silent_pings, _silent_pings * 5.0,
                            )
                        elif _silent_pings >= 3:
                            logger.warning(
                                "[UART] %d consecutive pings unanswered (%.0f s) "
                                "— possible TX line fault.",
                                _silent_pings, _silent_pings * 5.0,
                            )
                        else:
                            logger.debug("[UART] Console silent. Injecting ping to redraw prompt...")
                        self.serial.raw_write(active_ping_char)
                        last_rx_time = time.perf_counter()
                    # Heartbeat: a silent UART produces no events at all, which
                    # starves any consumer that only checks its own deadlines
                    # "on every event loop iteration".  Yield a local-only
                    # IdleTick roughly every second so those checks still run.
                    if time.perf_counter() - last_event_yield >= 1.0:
                        elapsed = round(time.perf_counter() - t_start, 3)
                        last_event_yield = time.perf_counter()
                        yield IdleTick(elapsed_s=elapsed)
                    continue

                if not chunk:
                    continue

                if _silent_pings > 0:
                    logger.debug("[UART] RX resumed after %d silent pings.", _silent_pings)
                    _silent_pings = 0
                last_rx_time = time.perf_counter()

                # Decode into the private local buffer; strip ANSI escape codes.
                _local_buf += self.ansi_pattern.sub(b'', chunk).decode('utf-8', errors='replace')
                elapsed = round(time.perf_counter() - t_start, 3)
                clean = _local_buf.encode('utf-8')

                if self.panic_pattern.search(clean):
                    ev = PanicDetected(
                        elapsed_s=elapsed,
                        raw_output=_local_buf[-500:],
                    )
                    bus.emit_uart_event(ev)
                    last_event_yield = time.perf_counter()
                    yield ev
                    return

                if autoboot_trigger and not autoboot_fired and autoboot_trigger in clean:
                    autoboot_fired = True
                    ev = AutobootWindowDetected(elapsed_s=elapsed)
                    bus.emit_uart_event(ev)
                    last_event_yield = time.perf_counter()
                    yield ev

                for ptype, pbytes in prompts.items():
                    if ptype not in detected_prompts and pbytes in clean:
                        detected_prompts.add(ptype)
                        logger.debug(f"[UART-FSM] Detected prompt '{ptype}' in buffer")
                        ev = PromptDetected(elapsed_s=elapsed, prompt_type=ptype)
                        bus.emit_uart_event(ev)
                        last_event_yield = time.perf_counter()
                        yield ev

                _local_buf = _local_buf.replace('\r\n', '\n').replace('\r', '\n')
                while '\n' in _local_buf:
                    line, _local_buf = _local_buf.split('\n', 1)
                    clean_line = line.strip()
                    if not clean_line:
                        continue
                    ev = BootDataReceived(elapsed_s=elapsed, line=clean_line)
                    bus.emit_uart_event(ev)
                    last_event_yield = time.perf_counter()
                    yield ev
                    found_keys = [k for k, v in pending_milestones.items() if v in clean_line]
                    for k in found_keys:
                        pending_milestones.pop(k)
                        ev2 = MilestoneReached(elapsed_s=elapsed, name=k)
                        bus.emit_uart_event(ev2)
                        last_event_yield = time.perf_counter()
                        yield ev2
        finally:
            self.serial.unsubscribe(rx_queue)

from pydantic import BaseModel, Field

class DeviceContext(BaseModel):
    active_rootfs: str = "UNKNOWN"
    crypto_data_mounted: bool = False
    active_boot_medium: str = "default"

    manifest: Optional[HardwareManifest] = None

    custom_data: Dict[str, Any] = Field(default_factory=dict)

class DutState(Enum):
    POWER_OFF = auto()
    ENERGIZED = auto()
    BOOTLOADER = auto()
    OS_USERLAND = auto()
    RECOVERY = auto()
    DIRTY = auto()


def _state_name(value: Any) -> str:
    """Normalizes a state reference to its name string.

    ``transitions`` sometimes hands back an Enum member (e.g. ``self.state``)
    and sometimes a plain name string (e.g. ``event.transition.source``).
    Pydantic would otherwise coerce an Enum straight to ``str(value)``
    (its numeric ``auto()`` value for ``DutState``), so normalize explicitly.
    """
    if isinstance(value, Enum):
        return value.name
    return str(value)

# ==========================================
# BOOT STRATEGIES (Strategy Pattern)
# ==========================================
class BootStrategy(ABC):
    """
    Encapsulates the boot sequence for a specific hardware boot architecture.

    Product teams can subclass this for custom boot flows (e.g., Android fastboot,
    UEFI Secure Boot) without modifying the core FSM.
    """

    @abstractmethod
    async def cold_boot_to_bootloader(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        """From POWER_OFF → BOOTLOADER. Board must be freshly energized."""
        ...

    @abstractmethod
    async def cold_boot_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        """From POWER_OFF → OS_USERLAND. Full boot sequence."""
        ...

    @abstractmethod
    async def resume_bootloader_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        """From BOOTLOADER → OS_USERLAND. Board is already at U-Boot."""
        ...


class AutobootStrategy(BootStrategy):
    """
    For boards with U-Boot autoboot countdown enabled.

    Intercepts the autoboot window by spamming interrupt characters,
    giving the FSM deterministic control over the boot process.
    """

    async def cold_boot_to_bootloader(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._do_energize)
        await fsm.event_wait_for_bootloader(intercept_autoboot=True)

    async def cold_boot_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._do_energize)
        await fsm.event_wait_for_bootloader(intercept_autoboot=True)
        await fsm.event_boot_from_bootloader_to_os()

    async def resume_bootloader_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        await fsm.event_boot_from_bootloader_to_os()


class TrapRebootStrategy(BootStrategy):
    """
    For boards with autoboot disabled (e.g., Secure Boot / HAB-locked).

    Cannot intercept U-Boot during normal boot. Instead, boots all the way
    to Linux, sets an fw_setenv trap, reboots, and catches U-Boot on the
    way back down.
    """

    async def cold_boot_to_bootloader(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._do_energize)
        await fsm.event_wait_for_os_shell()
        await fsm.finalize_os_boot()
        await anyio.to_thread.run_sync(fsm._set_uboot_trap_and_reboot)
        await fsm.event_wait_for_bootloader(intercept_autoboot=False)

    async def cold_boot_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._do_energize)
        await fsm.event_wait_for_os_shell()

    async def resume_bootloader_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._restore_uboot_trap)
        await fsm.event_boot_from_bootloader_to_os()


class RecoveryStrategy(ABC):
    """
    Encapsulates how to force the hardware into a low-level USB/Serial recovery mode
    (e.g., NXP Serial Downloader, STM32 DFU, TI UART boot).

    Two-phase lifecycle:
      trigger_recovery() — called before payload delivery: asserts the recovery strap
                           and power-cycles the board into BootROM / DFU mode.
      release_recovery() — called after payload delivery: releases the recovery strap
                           so the board can boot from the delivered payload.
    For GPIO-automated stations release_recovery() is a no-op (pin was cleared
    during trigger).  For manual-jumper stations it shows an operator prompt.
    """
    @abstractmethod
    async def trigger_recovery(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        ...

    @abstractmethod
    async def release_recovery(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        ...


class GpioRecoveryStrategy(RecoveryStrategy):
    """
    Forces recovery mode by asserting a physical GPIO pin (e.g., pulling BOOT_MODE high)
    while power cycling the board.
    """
    async def trigger_recovery(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        recovery_pin = fsm.cfg.gpio_recovery_pin
        if fsm.gpio and recovery_pin:
            await anyio.to_thread.run_sync(fsm.gpio.set_pin, recovery_pin, True)
            await anyio.to_thread.run_sync(fsm._do_energize)
            await anyio.sleep(fsm.cfg.recovery_latch_time_s)
            await anyio.to_thread.run_sync(fsm.gpio.set_pin, recovery_pin, False)
        else:
            if _is_headless():
                raise StateMachineError("Manual intervention required ('PRESS AND HOLD THE RECOVERY BUTTON') but running in headless/CI environment.")
            logger.warning("manual_action_required", instructions="PRESS AND HOLD THE RECOVERY BUTTON / SET JUMPER NOW.")
            try:
                await anyio.to_thread.run_sync(input, ">>> Press [ENTER] while holding the button... ")
            except (EOFError, KeyboardInterrupt): pass

            await anyio.to_thread.run_sync(fsm._do_energize)
            logger.warning("manual_action_required", instructions="POWER IS ON. YOU CAN NOW RELEASE THE RECOVERY BUTTON.")

    async def release_recovery(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        recovery_pin = fsm.cfg.gpio_recovery_pin
        if fsm.gpio and recovery_pin:
            # Automated stations: pin was already de-asserted inside trigger_recovery()
            # after recovery_latch_time_s.  Nothing to do here.
            logger.debug("release_recovery_gpio_no_op", pin=recovery_pin)
        else:
            # Manual-jumper stations: the strap is a persistent jumper, not a momentary
            # button.  It must stay installed while the board enumerates on USB and while
            # uuu pushes the payload.  Only NOW — after the SoC RAM is live — can the
            # operator safely remove it so the board boots the delivered payload.
            if _is_headless():
                raise StateMachineError("Manual intervention required ('REMOVE THE RECOVERY JUMPER / STRAP NOW') but running in headless/CI environment.")
            
            logger.warning("=" * 60)
            logger.warning("[RECOVERY] *** MANUAL ACTION REQUIRED ***")
            logger.warning("[RECOVERY] REMOVE THE RECOVERY JUMPER / STRAP NOW.")
            logger.warning("[RECOVERY] The board will boot from the payload once removed.")
            logger.warning("=" * 60)
            try:
                await anyio.to_thread.run_sync(input, ">>> Press [ENTER] once recovery jumper is removed... ")
            except (EOFError, KeyboardInterrupt):
                logger.warning("manual_prompt_interrupted", action="assuming_jumper_removed_continuing")




class ContextValidationStrategy(ABC):
    """
    Encapsulates OS-level validation (e.g., SWUpdate A/B partition checks, EVSE calibration).
    """
    @abstractmethod
    async def verify_linux_context(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        ...


class DefaultSWUpdateStrategy(ContextValidationStrategy):
    """
    Default OS validation strategy that parses the SWUpdate IPC socket to determine
    the active RootFS partition.
    """
    async def verify_linux_context(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        if not fsm.serial.is_connected:
            logger.debug("swupdate_context_check_skipped", reason="serial_not_connected_ssh_only_session")
            fsm.context.active_rootfs = "UNKNOWN"
            return

        logger.info("validating_ab_partitions_via_swupdate")
        res_sw = await fsm.serial.async_safe_run("swupdate -g", timeout_s=3.0, check_exit_code=False)

        if res_sw.ok:
            output = res_sw.stdout.strip()
            shell_prompt = fsm.cfg.os_shell_prompt
            lines = [l.strip() for l in output.split('\n') if l.strip() and "swupdate" not in l and shell_prompt not in l]
            fsm.context.active_rootfs = lines[-1] if lines else "UNKNOWN"
        else:
            logger.debug("swupdate_not_found_or_failed", action="setting_rootfs_to_unknown")
            fsm.context.active_rootfs = "UNKNOWN"

        crypto_part = fsm.cfg.storage_data_encrypted
        if crypto_part:
            mount_res = await fsm.serial.async_safe_run("mount | grep /data", timeout_s=3.0, check_exit_code=False)
            fsm.context.crypto_data_mounted = crypto_part in mount_res.stdout


class BaseDutStateMachine(ABC):
    STATES = [DutState.POWER_OFF, DutState.ENERGIZED, DutState.BOOTLOADER, DutState.OS_USERLAND, DutState.RECOVERY, DutState.DIRTY]
    # Imported from transports.constants — single source of truth shared with
    # UartKernelWatchdog and UartEventStream.
    PANIC_WATCHDOG: Pattern[bytes] = PANIC_PATTERN_B
    ANSI_ESCAPE_B: Pattern[bytes] = ANSI_ESCAPE_B
    # Use 'Any' here so type checkers don't yell when a project uses its own Enum
    state: Any

    def __init__(
        self,
        psu: Optional[ScpiPowerSupply],
        serial: EphemeralSerialClient,
        ssh: EphemeralSSHClient,
        cfg: StateMachineConfig,
        boot_profiler_cfg: Optional[BootProfilerConfig] = None,
        gpio: Optional[Any] = None,
        verbose: bool = False
    ):
        """Initializes the core Hardware State Machine context.

        Args:
            psu: Programmable power supply for cold-booting, if available.
            serial: UART transport used for early bootloader monitoring.
            ssh: High-speed Ethernet transport for OS-level interactions.
            cfg: Top-level TOML configuration parameters.
            boot_profiler_cfg: Analytics configuration for tracking boot phase duration.
            gpio: Optional hardware fixture controller for physical interaction.
            verbose: Enable printing of UART console output lines during boot checks.
        """
        self.psu = psu
        self.serial = serial
        self.ssh = ssh
        self.cfg = cfg
        self.boot_profiler_cfg = boot_profiler_cfg
        self.gpio = gpio
        self.verbose = verbose

        self.boot_metrics: Dict[str, float] = {}
        self.context = DeviceContext()
        self.context_validators: List[Callable[['BaseDutStateMachine'], None]] = []

        # Event-driven UART stream for boot monitoring
        self.event_stream = UartEventStream(
            serial=self.serial,
            ansi_pattern=self.ANSI_ESCAPE_B,
            panic_pattern=self.PANIC_WATCHDOG,
        )

        # Auto-select boot strategy based on hardware config
        self.boot_strategy: BootStrategy = (
            AutobootStrategy() if self.cfg.autoboot_enabled
            else TrapRebootStrategy()
        )
        self.recovery_strategy: RecoveryStrategy = GpioRecoveryStrategy()
        self.context_strategy: ContextValidationStrategy = DefaultSWUpdateStrategy()
        logger.debug("boot_strategy_selected", strategy=type(self.boot_strategy).__name__)

        logger.debug("initializing_fsm", psu_present=self.psu is not None, gpio_present=self.gpio is not None)

        machine_kwargs = {
            "model": self,
            "states": self.STATES,
            "initial": DutState.DIRTY,
            "send_event": True,
            "after_state_change": '_record_timestamp'
        }
        if HAS_GRAPHVIZ:
            machine_kwargs["title"] = "MES Hardware State Graph"
            machine_kwargs["show_conditions"] = True

        self.machine = Machine(**machine_kwargs)

        self.machine.add_transition('power_off', '*', DutState.POWER_OFF, before='_hw_power_off')
        self.machine.add_transition('energize', '*', DutState.ENERGIZED, before='_hw_energize')
        self.machine.add_transition('boot_to_bootloader', '*', DutState.BOOTLOADER, before='_hw_boot_to_bootloader')
        self.machine.add_transition('boot_to_os', '*', DutState.OS_USERLAND, before='_hw_boot_to_os')
        self.machine.add_transition('boot_to_recovery', '*', DutState.RECOVERY, before='_hw_to_recovery')
        
        async def _log_dirty(e):
            logger.warning("state_marked_dirty")
            
        self.machine.add_transition('mark_dirty', '*', DutState.DIRTY, before=_log_dirty)

        self._register_custom_states()

    def _register_custom_states(self) -> None:
        """Override this in project subclasses to add custom states and transitions.
        This runs automatically during __init__ to patch the FSM.
        logger.info("[EVSE FSM] Injecting custom EVSE hardware states...")
        # Add the new states to the existing machine
        self.machine.add_state(EvseState.CALIBRATION_MODE)
        self.machine.add_state(EvseState.FACTORY_FLASH_MODE)
        # Map the transition triggers to your custom physical hooks
        self.machine.add_transition(
            trigger='boot_to_calibration',
            source='*', # Can transition from anywhere
            dest=EvseState.CALIBRATION_MODE,
            before='_hw_to_calibration'
        )
        """
        pass



    def register_context_validator(self, validator_func: Callable[['BaseDutStateMachine'], None]) -> None:
        """Allows test fixtures to seamlessly inject custom OS validation methods."""
        if validator_func not in self.context_validators:
            self.context_validators.append(validator_func)

    def _record_timestamp(self, event: EventData) -> None:
        from pytest_mes_core.events import bus, StateChanged
        ev = StateChanged(
            fsm_name=self.__class__.__name__,
            old_state=_state_name(event.transition.source),
            new_state=_state_name(self.state),
            trigger=event.event.name,
            timestamp=time.time()
        )
        bus.emit_state_event(event=ev)

    @abstractmethod
    async def _hw_power_off(self, event: EventData) -> None: pass
    @abstractmethod
    async def _hw_energize(self, event: EventData) -> None: pass
    @abstractmethod
    async def _hw_boot_to_bootloader(self, event: EventData) -> None: pass
    @abstractmethod
    async def _hw_boot_to_os(self, event: EventData) -> None: pass
    @abstractmethod
    async def _hw_to_recovery(self, event: EventData) -> None: pass


class EmbeddedLinuxStateMachine(BaseDutStateMachine):
    """
    Concrete FSM for embedded Linux SoCs (i.MX, AM62x, STM32MP, etc.).

    Uses the event-driven UartEventStream for boot monitoring and delegates
    boot sequences to the BootStrategy selected at init time.
    """

    def _probe_hardware_state(self) -> DutState:
        """Unified, multi-domain state probe (Power -> Network -> Serial).

        Checks the actual hardware state by prioritizing the fastest, least
        intrusive methods before falling back to serial polling.
        1. Power Domain: Measures PSU current to instantly detect POWER_OFF.
        2. Network Domain: Tests the SSH port to instantly detect OS_USERLAND.
        3. Serial Domain: Polls UART for Bootloader, Login, or Shell prompts.

        Returns:
            DutState: The dynamically detected physical state of the target device.
        """
        # 1. Power Domain Probe (Fastest check)
        if self.psu and hasattr(self.psu, "measure_current"):
            try:
                # Robustness: Debounce current measurement over 3 samples to ignore transient dips
                samples = [float(self.psu.measure_current()) for _ in range(3)]
                avg_current = sum(samples) / len(samples)
                power_threshold = self.cfg.power_off_threshold_a
                
                if avg_current < power_threshold:
                    logger.debug("probe_result", domain="power", current_a=avg_current, threshold_a=power_threshold, state="POWER_OFF")
                    return DutState.POWER_OFF
            except Exception as e:
                logger.debug("psu_current_measurement_failed", error=str(e))

        # 2. Network Domain Probe (Non-intrusive)
        if self.ssh.is_connected:
            logger.debug("probe_result", domain="network", state="OS_USERLAND", reason="Existing SSH session active")
            return DutState.OS_USERLAND

        # Attempt a quick TCP socket test + SSH Banner Grab.
        # This bypasses the long @retry loops in the transport's connect() method.
        try:
            with socket.create_connection((self.ssh.ip_address, self.ssh.cfg.port), timeout=1.0) as sock:
                # Robustness: Wait for the OpenSSH banner to confirm daemon is actually ready
                sock.settimeout(1.0)
                banner = sock.recv(1024).decode('utf-8', errors='ignore')
                if "SSH-2.0" in banner:
                    logger.info("probe_result", domain="network", state="OS_USERLAND", reason="SSH Daemon ready")
                    return DutState.OS_USERLAND
        except (OSError, socket.timeout):
            pass  # Port closed or daemon not ready, fall through to Serial probe

        # 3. Serial Domain Probe (Fallback)
        if not self.serial.is_connected:
            self.serial.connect()
            time.sleep(0.1)

        # Robustness: Passive Listen First.
        # Don't blindly inject \r\n yet! If the board is autobooting, an injected \n
        # will violently drop it into the U-Boot shell by accident.
        time.sleep(0.3)
        passive_chunk = self.serial.raw_read_chunk()
        if passive_chunk:
            # Board is actively streaming — inspect before declaring ENERGIZED
            # (it may already be at a shell or bootloader prompt).
            clean_passive = self.ANSI_ESCAPE_B.sub(b'', passive_chunk)
            if self.cfg.os_shell_prompt.encode() in clean_passive:
                return DutState.OS_USERLAND
            if self.cfg.bootloader_prompt.encode() in clean_passive:
                return DutState.BOOTLOADER
            logger.debug("probe_result", domain="serial", bytes_received=len(passive_chunk), state="ENERGIZED")
            return DutState.ENERGIZED

        # BUG-3 fix: two-pass ping instead of broken triple-nested structure.
        # Pass 1: fast (0.4 s) — catches agetty / idle login prompts.
        # Pass 2: slow (2.0 s) — accounts for UART chip swallowing first byte.
        resp = bytearray()
        for wait_s in (0.4, 2.0):
            if resp:
                break
            self.serial.flush_buffers()
            self.serial.raw_write(b"\r\n")
            time.sleep(wait_s)
            while True:
                chunk = self.serial.raw_read_chunk()
                if not chunk:
                    break
                resp.extend(chunk)
                time.sleep(0.05)
            if not resp:
                logger.debug("probe_pass_silent", wait_s=wait_s, action="retrying")

        # Strict ANSI stripping applied centrally
        clean_resp = self.ANSI_ESCAPE_B.sub(b'', resp)

        shell_prompt = self.cfg.os_shell_prompt.encode('utf-8')
        login_prompt = self.cfg.os_login_prompt.encode('utf-8')
        uboot_prompt = self.cfg.bootloader_prompt.encode('utf-8')

        if shell_prompt in clean_resp:
            return DutState.OS_USERLAND
        elif login_prompt in clean_resp:
            return DutState.ENERGIZED
        elif uboot_prompt in clean_resp:
            return DutState.BOOTLOADER
        elif len(clean_resp) > 0:
            return DutState.ENERGIZED

        logger.debug("probe_clean_response", response=clean_resp)
        return DutState.POWER_OFF

    def _do_soft_reboot(self) -> None:
        """
        Consolidates desk-mode soft reboots.
        """
        logger.info("desk_mode_soft_reboot_attempt")
        self.serial.write_line(self.cfg.os_user or "root")
        time.sleep(0.5)
        if self.cfg.os_password:
            self.serial.write_line(self.cfg.get_os_password() or "", sensitive=True)
            time.sleep(0.5)
        self.serial.write_line("reboot")

    # =========================================================================
    # STATE RESOLUTION
    # =========================================================================

    def _align_to_physical_state(self, force: bool = False) -> None:
        """
        Probes UART to detect actual hardware state and aligns the FSM.
        Uses machine.set_state() to ensure transitions library tracks the change.

        Probe is skipped when:
        - ``force`` is False AND the PSU is present AND state is not DIRTY
          (PSU presence means we can trust the power-cycle history).
        - ``force`` is False AND the state is RECOVERY, BOOTLOADER, or POWER_OFF
          (these were explicitly set by a transition command; we know the hardware).

        Probing from an explicitly-commanded state is dangerous: a false POWER_OFF
        result would bypass the _do_power_off() call in the cold-boot path, causing
        the operator to never be asked to unplug the board.

        Args:
            force: If True, probes regardless of current state or PSU presence.
        """
        if not force:
            if self.psu is not None and self.state != DutState.DIRTY:
                return  # PSU present: power-cycle history is tracked, trust the FSM
            if self.state in (DutState.RECOVERY, DutState.BOOTLOADER, DutState.POWER_OFF):
                # Explicitly-commanded states — we know what the hardware is doing.
                logger.debug("probe_skipped", reason=f"explicitly_commanded_state_{self.state.name}")
                return

        logger.debug("probing_hardware_state")
        detected_state = self._probe_hardware_state()

        state_labels = {
            DutState.OS_USERLAND: "Detected OS Shell",
            DutState.ENERGIZED: "Detected output/login",
            DutState.BOOTLOADER: "Detected Bootloader",
            DutState.POWER_OFF: "Silence",
        }
        logger.info("aligning_physical_state", detected_state=detected_state.name, uart_probe=state_labels.get(detected_state, 'Unknown'))
        self.machine.set_state(detected_state)

    # =========================================================================
    # PHYSICAL PRIMITIVES
    # =========================================================================

    def _do_apply_bootstrap(self, medium: str) -> None:
        """Applies physical hardware bootstrap constraints (e.g., pulling GPIO pins).

        Args:
            medium: The boot medium requested ("default", "emmc", "sd", etc).
        """
        if not medium:
            medium = "default"
        straps = self.cfg.boot_straps_gpio_map.get(medium)

        if self.gpio:
            if straps:
                for pin, state in straps.items():
                    self.gpio.set_pin(pin, state)
                time.sleep(0.5)
            elif medium != "default":
                # BUG-5 fix: warn when GPIO controller present but no strap map
                logger.warning("no_gpio_strap_map_for_medium", medium=medium,
                               hint="Add boot_straps_gpio_map to your TOML config")
            return

        # No GPIO controller — prompt the operator
        if _is_headless():
            raise StateMachineError(f"Manual hardware intervention required (SET BOOT STRAPS TO {medium.upper()}) but running in headless/CI environment.")
        
        logger.warning("=" * 60)
        if medium == "default":
            logger.warning("[MANUAL ACTION] RESTORE HARDWARE BOOT STRAP PINS / DIP SWITCHES TO DEFAULT.")
        else:
            logger.warning(f"[MANUAL ACTION] SET HARDWARE BOOT STRAP PINS TO: **{medium.upper()}**")
        logger.warning("=" * 60)
        try:
            input(">>> Press [ENTER] once configured... ")
        except (EOFError, KeyboardInterrupt):
            logger.warning("manual_prompt_interrupted", action="continuing_without_confirmation")
            time.sleep(2.0)

    def _do_hardware_reset(self) -> None:
        """Toggles the physical RESET pin on the board via GPIO.

        If no reset pin is defined, falls back to a hard power cycle.
        """
        reset_pin = self.cfg.gpio_reset_pin
        if self.gpio and reset_pin:
            # Robustness: Prevent backpowering the SoC through the reset pin
            if self.state == DutState.POWER_OFF:
                logger.warning("hardware_reset_skipped", reason="board_is_power_off_preventing_backpower")
                return

            logger.warning("firing_hardware_reset_pin", reset_pin=reset_pin)
            self.gpio.set_pin(reset_pin, True)
            time.sleep(0.5)
            self.gpio.set_pin(reset_pin, False)
            time.sleep(0.5)
        else:
            logger.debug("no_hardware_reset_pin", fallback="hard_power_cycle")
            self._do_power_off()
            self._do_energize()

    def _do_power_off(self) -> None:
        """Executes a hard power drop using the PSU.

        If no PSU is connected, pauses FSM execution and prompts the user
        to manually unplug the power cable.
        """
        logger.debug("executing_hard_power_drop")
        if self.ssh.is_connected: self.ssh.disconnect()

        cm = self.context.active_boot_medium
        self.context = DeviceContext()
        self.context.active_boot_medium = cm

        if self.psu:
            self.psu.set_voltage(0.0)
            self.psu.disable_output()
            time.sleep(2.0)
        else:
            if _is_headless():
                raise StateMachineError("Manual hardware intervention required (UNPLUG 12V POWER) but running in headless/CI environment.")
                
            logger.warning("manual_action_required", instructions="UNPLUG THE 12V POWER FROM THE BOARD NOW.")
            try: input(">>> Press [ENTER] once powered off... ")
            except (EOFError, KeyboardInterrupt):
                logger.warning("manual_prompt_interrupted", action="assuming_power_off_continuing")
                time.sleep(2.0)

    def _do_energize(self) -> None:
        """Applies physical voltage to the board and captures inrush current.

        If no PSU is connected, pauses FSM execution and prompts the user
        to manually plug in the power cable.
        """
        logger.info("applying_raw_power", medium=self.context.active_boot_medium)

        if self.psu:
            if hasattr(self.psu, 'default_voltage'):
                self.psu.set_voltage(self.psu.default_voltage)
            self.psu.enable_output()
            if hasattr(self.psu, "measure_current"):
                t_end = time.perf_counter() + 1.0
                max_i = 0.0
                while time.perf_counter() < t_end:
                    try:
                        i = float(self.psu.measure_current())
                        if i > max_i: max_i = i
                    except Exception:
                        pass
                self.boot_metrics["inrush_current_a"] = round(max_i, 3)
            else:
                time.sleep(1.0)
        else:
            if _is_headless():
                raise StateMachineError("Manual hardware intervention required (PLUG IN 12V POWER) but running in headless/CI environment.")
                
            logger.warning("manual_action_required", instructions="PLUG IN THE 12V POWER NOW.")
            try: input(">>> Press [ENTER] once power is applied... ")
            except (EOFError, KeyboardInterrupt):
                logger.warning("manual_prompt_interrupted", action="assuming_power_applied_continuing")
                time.sleep(2.0)

        # Securely re-bind the serial port to recover the file descriptor.
        # If the USB-Serial adapter is physically on the board, it drops and re-enumerates during a power cycle.
        self._rebind_serial_port()

    @retry(stop=stop_after_attempt(50), wait=wait_fixed(0.1), reraise=True, before_sleep=before_sleep_log(logger, logging.DEBUG))
    def _rebind_serial_port(self) -> None:
        """Attempts to reconnect the serial port after a hardware power cycle.

        After connect() opens the port, the USB-UART adapter may still have
        buffered TX bytes from the previous session (e.g. a failed heartbeat
        command).  We flush both directions immediately after open so those
        stale bytes never reach the board's freshly-started getty.
        """
        self.serial.disconnect()
        self.serial.connect()
        # Give the adapter's FIFO a brief window to drain into the RX queue
        # (so flush_buffers discards them rather than open() ingesting them).
        time.sleep(0.2)
        try:
            if self.serial.ser and self.serial.ser.is_open:
                self.serial.ser.reset_output_buffer()  # drop any pending TX bytes
                self.serial.flush_buffers()            # drain stale RX bytes
        except Exception:
            pass

    def _set_uboot_trap_and_reboot(self) -> None:
        logger.info("hot_patching_uboot_env")
        # Robustness: We MUST check exit codes here. Silently failing fw_setenv will brick the FSM trap loop.
        self.transport.safe_run("fw_setenv mes_prev_bootcmd \"$(fw_printenv -n bootcmd)\"", timeout_s=3.0, check_exit_code=True)
        self.transport.safe_run("fw_setenv bootcmd 'echo MES Framework Trap'", timeout_s=3.0, check_exit_code=True)
        try:
            self.transport.safe_run("reboot", timeout_s=2.0, check_exit_code=False)
        except TransportError:
            pass  # connection dropping during reboot is expected
        if self.ssh.is_connected:
            self.ssh.disconnect()  # session is dead either way; avoid a stale socket

    def _restore_uboot_trap(self) -> None:
        logger.info("restoring_original_uboot_env")
        prompt = self.cfg.bootloader_prompt
        self.serial.safe_run('setenv bootcmd "${mes_prev_bootcmd}"', expected_prompt=prompt, timeout_s=3.0)
        self.serial.safe_run('setenv mes_prev_bootcmd', expected_prompt=prompt, timeout_s=3.0)
        self.serial.safe_run('saveenv', expected_prompt=prompt, timeout_s=5.0)

    # =========================================================================
    # EVENT-DRIVEN BOOT METHODS
    # =========================================================================

    async def event_wait_for_bootloader(self, intercept_autoboot: bool) -> None:
        """Asynchronous event-driven bootloader interception."""
        import anyio
        logger.info("hunting_for_bootloader_prompt")

        blast_bytes = self.cfg.bootloader_interrupt_char.encode('utf-8')
        prompts = {
            "bootloader": self.cfg.bootloader_prompt.encode('utf-8'),
            "trap": b"MES Framework Trap",
        }
        autoboot_trigger = (
            self.cfg.bootloader_interrupt_pattern.encode('utf-8')
            if intercept_autoboot else None
        )

        ping_char = b"\r\n" if intercept_autoboot else None

        stream = self.event_stream.open_async(
            prompts=prompts,
            timeout_s=self.cfg.cold_boot_timeout_s,
            autoboot_trigger=autoboot_trigger,
            active_ping_char=ping_char,
        )
        try:
            async for event in stream:
                if isinstance(event, PanicDetected):
                    raise KernelPanicError(
                        f"Kernel panic during Bootloader routing:\n{event.raw_output}"
                    )

                elif isinstance(event, AutobootWindowDetected):
                    logger.info("autoboot_window_detected_sniping")
                    # Blast the interrupt character 3x to suppress the countdown.
                    # Use run_sync so raw_write doesn't block the event loop.
                    for _ in range(3):
                        await anyio.to_thread.run_sync(
                            functools.partial(self.serial.raw_write, blast_bytes)
                        )
                        await anyio.sleep(0.05)

                elif isinstance(event, PromptDetected) and event.prompt_type in ("bootloader", "trap"):
                    # Stabilise the prompt: send \n, wait, drain stale bytes.
                    await anyio.to_thread.run_sync(
                        functools.partial(self.serial.raw_write, b"\n")
                    )
                    await anyio.sleep(0.1)
                    await anyio.to_thread.run_sync(self.serial.flush_buffers)

                    res = await anyio.to_thread.run_sync(
                        functools.partial(self.serial.safe_run, "echo MES_SYNC", expected_prompt=self.cfg.bootloader_prompt, timeout_s=3.0)
                    )

                    if "MES_SYNC" not in res.stdout:
                        raise BootloaderSyncError("Failed to synchronize with Bootloader prompt after detection.")
                    logger.info("bootloader_intercepted_successfully")
                    return

                elif isinstance(event, BootDataReceived):
                    if self.verbose:
                        logger.info("uart_rx", data=event.line)
                    else:
                        logger.debug("uart_rx", data=event.line)
        finally:
            await stream.aclose()

        raise BootloaderTimeoutError(
            f"Failed to intercept Bootloader within {self.cfg.cold_boot_timeout_s}s timeout."
        )

    async def event_boot_from_bootloader_to_os(self) -> None:
        """Send the boot command from U-Boot and wait for OS shell via async event stream."""
        import anyio
        logger.info("commanding_os_boot", boot_cmd=self.cfg.bootloader_boot_cmd)
        # flush_buffers and raw_write are sync primitives — offload to a thread
        # so the event loop is never blocked.  This also ensures compatibility
        # with any DutTransport implementation (not just EphemeralSerialClient).
        await anyio.to_thread.run_sync(self.serial.flush_buffers)
        boot_cmd_bytes = f"{self.cfg.bootloader_boot_cmd}\n".encode()
        await anyio.to_thread.run_sync(
            functools.partial(self.serial.raw_write, boot_cmd_bytes)
        )
        await self.event_wait_for_os_shell(flush=False)

    async def event_wait_for_os_shell(self, flush: bool = True) -> None:
        """Asynchronous event-driven OS boot monitor."""
        import anyio
        logger.info("waiting_for_linux_userland")
        self.boot_metrics.clear()

        prompts: Dict[str, bytes] = {
            "shell": self.cfg.os_shell_prompt.encode('utf-8'),
            "login": self.cfg.os_login_prompt.encode('utf-8'),
            "bootloader": self.cfg.bootloader_prompt.encode('utf-8'),
        }
        if self.cfg.os_password:
            prompts["password"] = self.cfg.os_password_prompt.encode('utf-8')

        milestones = (
            self.boot_profiler_cfg.milestones.copy()
            if self.boot_profiler_cfg else {}
        )

        stream = self.event_stream.open_async(
            prompts=prompts,
            timeout_s=self.cfg.cold_boot_timeout_s,
            milestones=milestones,
            flush=flush,
            active_ping_char=b"\n",
        )
        try:
            async for event in stream:
                if isinstance(event, PanicDetected):
                    raise KernelPanicError("Device kernel panicked during OS boot sequence.")

                elif isinstance(event, MilestoneReached):
                    self.boot_metrics[f"t_boot_{event.name}_s"] = event.elapsed_s

                elif isinstance(event, PromptDetected):
                    if event.prompt_type == "shell":
                        self.boot_metrics["t_boot_total_to_shell_s"] = event.elapsed_s
                        logger.info("auto_login_shell_reached", elapsed_s=event.elapsed_s)
                        return

                    elif event.prompt_type == "login":
                        self.boot_metrics["t_boot_total_to_login_s"] = event.elapsed_s
                        await anyio.sleep(0.1)
                        await anyio.to_thread.run_sync(self.serial.write_line, self.cfg.os_user or "root")
                        self.serial.parser.clear_buffer()

                    elif event.prompt_type == "password":
                        await anyio.sleep(0.1)
                        await anyio.to_thread.run_sync(
                            functools.partial(self.serial.write_line, self.cfg.get_os_password() or "", sensitive=True)
                        )
                        self.serial.parser.clear_buffer()

                    elif event.prompt_type == "bootloader":
                        logger.warning("interrupted_autoboot_injecting_boot_command")
                        await anyio.to_thread.run_sync(self.serial.write_line, "boot")

                elif isinstance(event, BootDataReceived):
                    if self.verbose:
                        logger.info("uart_rx", data=event.line)
                    else:
                        logger.debug("uart_rx", data=event.line)
        finally:
            await stream.aclose()

        raise TransportTimeoutError("Timed out waiting for Linux Shell prompt.")

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_fixed(1.0),
        reraise=True,
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _connect_ssh_with_retry(self) -> None:
        """Retrying SSH connect — sshd may not be ready the instant the shell prompt appears."""
        self.ssh.connect()

    async def finalize_os_boot(self) -> None:
        """Async variant of finalize_os_boot.

        Applies the same SSH retry logic as the sync path: sshd may not be
        listening the instant the shell prompt appears, so we retry for up to
        10 seconds with 1-second intervals before propagating the error.
        """
        if self.ssh.is_connected:
            return
        await self._verify_linux_context()

        if self.serial.is_connected:
            res_sysd = await anyio.to_thread.run_sync(
                functools.partial(self.serial.safe_run, "systemd-analyze time", timeout_s=5.0, check_exit_code=False)
            )
            if res_sysd.ok and "Startup finished in" in res_sysd.stdout:
                try:
                    k_match = re.search(r'([\d\.]+)s\s*\(kernel\)', res_sysd.stdout)
                    u_match = re.search(r'([\d\.]+)s\s*\(userspace\)', res_sysd.stdout)
                    if k_match: self.boot_metrics["t_systemd_kernel_s"] = float(k_match.group(1))
                    if u_match: self.boot_metrics["t_systemd_userspace_s"] = float(u_match.group(1))
                except Exception:
                    pass

        # BUG-4 async fix: use the same retrying SSH connect as the sync path.
        logger.info("establishing_primary_ssh_transport")
        await anyio.to_thread.run_sync(self._connect_ssh_with_retry)


    def verify_heartbeat(self) -> bool:
        """Ping the OS state via heartbeat to verify it's still alive.

        Returns:
            bool: True if the device successfully echoes the heartbeat payload, False otherwise.
        """
        if self.state == DutState.OS_USERLAND and self.ssh and not self.ssh.is_connected:
            try:
                logger.debug("attempting_ssh_reconnect_for_heartbeat")
                self.ssh.connect()
            except Exception:
                pass

        if not self.transport.is_connected:
            return False
        logger.debug("verifying_heartbeat", transport=self.transport.__class__.__name__)
        try:
            res = self.transport.safe_run("echo MES_HEARTBEAT", timeout_s=5.0, check_exit_code=False)
            return "MES_HEARTBEAT" in res.stdout
        except Exception as e:
            logger.debug("heartbeat_failed", reason=str(e))
            return False

    async def async_verify_heartbeat(self, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.verify_heartbeat, *args, **kwargs)

    # =========================================================================
    # SOTA CONTEXT VALIDATION
    # =========================================================================

    async def _verify_linux_context(self) -> None:
        """Executes dynamically injected Validators, or defaults to the selected Context Strategy.

        Parses the current A/B partition configuration and populates
        the DeviceContext. If custom validators are registered, it executes them
        sequentially instead.
        """
        import inspect
        if self.context_validators:
            logger.info("executing_dynamic_context_validators", count=len(self.context_validators))
            for validator in self.context_validators:
                try:
                    if inspect.iscoroutinefunction(validator):
                        await validator(self)
                    else:
                        validator(self)
                except Exception as e:
                    logger.error("custom_context_validator_failed", error=str(e))
            return

        await self.context_strategy.verify_linux_context(self)

    # =========================================================================
    # FSM 'BEFORE' TRANSITION HOOKS
    # =========================================================================

    async def _hw_power_off(self, event: EventData) -> None:
        import anyio
        await anyio.to_thread.run_sync(self._align_to_physical_state)
        if self.state == DutState.POWER_OFF: return
        await anyio.to_thread.run_sync(self._do_power_off)

    async def _hw_energize(self, event: EventData) -> None:
        import anyio
        await anyio.to_thread.run_sync(self._align_to_physical_state)
        target_medium = event.kwargs.get("medium", self.context.active_boot_medium)
        needs_strap_change = target_medium != self.context.active_boot_medium

        if needs_strap_change or self.state == DutState.RECOVERY:
            await anyio.to_thread.run_sync(self._do_power_off)
            await anyio.to_thread.run_sync(self._do_apply_bootstrap, target_medium)
            self.context.active_boot_medium = target_medium
            await anyio.to_thread.run_sync(self._do_energize)
            return

        if self.state in [DutState.ENERGIZED, DutState.BOOTLOADER, DutState.OS_USERLAND]: return
        await anyio.to_thread.run_sync(self._do_apply_bootstrap, target_medium)
        self.context.active_boot_medium = target_medium
        await anyio.to_thread.run_sync(self._do_energize)

    async def _hw_boot_to_bootloader(self, event: EventData) -> None:
        import anyio
        await anyio.to_thread.run_sync(self._align_to_physical_state)
        target_medium = event.kwargs.get("medium", self.context.active_boot_medium)
        needs_strap_change = target_medium != self.context.active_boot_medium

        if needs_strap_change or self.state == DutState.RECOVERY:
            await anyio.to_thread.run_sync(self._do_power_off)
            await anyio.to_thread.run_sync(self._do_apply_bootstrap, target_medium)
            self.context.active_boot_medium = target_medium
            await self.boot_strategy.cold_boot_to_bootloader(self)
            return

        if self.state == DutState.BOOTLOADER:
            return

        if self.state == DutState.OS_USERLAND:
            if self.cfg.autoboot_enabled:
                await self._async_reboot_dut()
            else:
                await anyio.to_thread.run_sync(self._set_uboot_trap_and_reboot)
        elif self.state == DutState.ENERGIZED:
            if not self.psu:
                if not self.cfg.autoboot_enabled:
                    logger.warning(
                        "soft_reboot_without_autoboot_trap",
                        reason="autoboot_disabled_and_no_trap_installed",
                        hint="bootloader interception may fail; a full trap flow from a login prompt is out of scope",
                    )
                await anyio.to_thread.run_sync(self._do_soft_reboot)
            else:
                await anyio.to_thread.run_sync(self._do_power_off)
                await self.boot_strategy.cold_boot_to_bootloader(self)
                return
        else:
            await self.boot_strategy.cold_boot_to_bootloader(self)
            return

        await self.event_wait_for_bootloader(intercept_autoboot=self.cfg.autoboot_enabled)

    async def _hw_boot_to_os(self, event: EventData) -> None:
        import anyio
        self.boot_metrics.clear()
        await anyio.to_thread.run_sync(self._align_to_physical_state)

        target_medium = event.kwargs.get("medium", self.context.active_boot_medium)
        needs_strap_change = target_medium != self.context.active_boot_medium

        if needs_strap_change:
            logger.info("boot_medium_change_requested", target_medium=target_medium, action="forcing_hard_reboot")

        if self.state == DutState.OS_USERLAND:
            if await self._try_resume_existing_os():
                await self.finalize_os_boot()
                return
            await anyio.to_thread.run_sync(self._do_hardware_reset)
            self.machine.set_state(DutState.ENERGIZED)  # board is freshly rebooting -> treat as hot-login candidate

        if self.state == DutState.ENERGIZED:
            if await self._try_hot_login():
                return
            await anyio.to_thread.run_sync(self._do_hardware_reset)
            self.machine.set_state(DutState.DIRTY)       # escalate: full cold boot below

        if self.state in [DutState.RECOVERY, DutState.DIRTY, DutState.POWER_OFF]:
            if self.state != DutState.POWER_OFF:
                await anyio.to_thread.run_sync(self._do_power_off)
            await anyio.to_thread.run_sync(self._do_apply_bootstrap, target_medium)
            self.context.active_boot_medium = target_medium
            await self.boot_strategy.cold_boot_to_os(self)
            await self.finalize_os_boot()
            return

        if self.state == DutState.BOOTLOADER:
            await self.boot_strategy.resume_bootloader_to_os(self)
            await self.finalize_os_boot()
            return

    @property
    def transport(self):
        """Returns the primary active transport.

        Prioritizes SSH for OS-level interactions, falling back to serial
        if SSH is unavailable.
        """
        if self.ssh and self.ssh.is_connected:
            return self.ssh
        return self.serial

    async def _async_reboot_dut(self) -> None:
        """Reboot via the best available transport; tolerate the pipe dying mid-command."""
        t = self.transport  # prefers connected SSH, falls back to serial
        try:
            await t.async_safe_run("reboot", timeout_s=2.0, check_exit_code=False)
        except TransportError:
            pass  # connection dropping during reboot is expected
        if self.ssh.is_connected:
            self.ssh.disconnect()  # session is dead either way; avoid a stale socket

    async def _try_resume_existing_os(self) -> bool:
        """Liveness check for an existing OS_USERLAND session. Returns True if board is alive.
        """
        import anyio
        logger.debug("verifying_ssh_heartbeat_existing_os")
        try:
            await anyio.to_thread.run_sync(self._connect_ssh_with_retry)
            res = await self.ssh.async_safe_run("echo MES_HEARTBEAT", timeout_s=2.0)
            if "MES_HEARTBEAT" in res.stdout:
                return True
        except Exception as e:
            logger.debug("ssh_liveness_check_failed", reason=str(e))

        logger.debug("verifying_uart_heartbeat_existing_os")

        if self.serial.is_connected:
            try:
                res = await self.serial.async_safe_run("echo MES_HEARTBEAT", timeout_s=2.0, check_exit_code=False)
            except TransportConnectionError:
                logger.warning("uart_heartbeat_failed", reason="serial_port_closed_mid_run", action="marking_dirty")
                return False

            if "MES_HEARTBEAT" in res.stdout:
                return True

            logger.warning("uart_heartbeat_failed", reason="os_is_a_zombie", action="marking_dirty")
            try:
                if self.serial.ser and self.serial.ser.is_open:
                    await anyio.to_thread.run_sync(self.serial.ser.write, b'\x03\x03\r\n')
                    await anyio.to_thread.run_sync(self.serial.ser.flush)
                    await anyio.to_thread.run_sync(self.serial.ser.reset_output_buffer)
                    await anyio.to_thread.run_sync(self.serial.flush_buffers)
                    logger.debug("uart_tx_fifo_flushed_before_power_cycle")
            except Exception as flush_err:
                logger.debug("uart_flush_skipped", reason=str(flush_err))
            return False

        return False

    async def _try_hot_login(self) -> bool:
        """Attempt hot-login from ENERGIZED (Actively Booting or at Login) state."""
        logger.info("hot_login_from_energized")
        try:
            await self.event_wait_for_os_shell(flush=False)
            await self.finalize_os_boot()
            return True
        except TransportTimeoutError:
            logger.warning("hot_login_failed", reason="shell_prompt_not_reached", action="marking_dirty")
            return False

    async def _hw_to_recovery(self, event: EventData) -> None:
        import anyio
        await anyio.to_thread.run_sync(self._align_to_physical_state)
        if self.state == DutState.RECOVERY: return

        logger.info("routing_to_hardware_recovery", action="power_cycle_required")
        if self.state != DutState.POWER_OFF: 
            await anyio.to_thread.run_sync(self._do_power_off)

        await self.recovery_strategy.trigger_recovery(self)
        await anyio.sleep(2.0)

    async def release_recovery(self) -> None:
        """Release the recovery strap after payload delivery."""
        logger.info("releasing_recovery_strap", strategy=type(self.recovery_strategy).__name__)
        await self.recovery_strategy.release_recovery(self)
