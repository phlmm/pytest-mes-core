import time
import socket
import re
import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional, Pattern, Any, Callable, List, Generator
from dataclasses import dataclass, field
from transitions import Machine, EventData
from enum import Enum, auto
from tenacity import retry, stop_after_attempt, wait_fixed, before_sleep_log

from pytest_mes_core.config import StateMachineConfig, BootProfilerConfig
from pytest_mes_core.instruments import ScpiPowerSupply
from pytest_mes_core.transports import EphemeralSerialClient, EphemeralSSHClient
from pytest_mes_core.transports import TransportTimeoutError, TransportConnectionError
from pytest_mes_core.manifest import HardwareManifest

try:
    from transitions.extensions import GraphMachine as Machine
    HAS_GRAPHVIZ = True
except ImportError:
    from transitions import Machine
    HAS_GRAPHVIZ = False

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
    PanicDetected, MilestoneReached, BootDataReceived, bus
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
        Opens the UART event stream and yields typed events.

        Each prompt type is yielded at most once. Milestones are yielded once
        and removed from the pending set. PanicDetected terminates the generator.

        Args:
            prompts: Map of prompt_type → bytes pattern to watch for.
            timeout_s: Maximum wall-clock seconds before the stream ends.
            milestones: Optional boot profiler milestone map (name → substring).
            autoboot_trigger: Optional bytes pattern for autoboot countdown detection.

        Yields:
            UartEvent subclasses in the order they are detected.
        """
        t_start = time.perf_counter()
        pending_milestones = dict(milestones) if milestones else {}
        autoboot_fired = False

        if flush:
            self.serial.flush_buffers()

        last_rx_time = time.perf_counter()

        while time.perf_counter() - t_start < timeout_s:
            chunk = self.serial.raw_read_chunk()
            if not chunk:
                if active_ping_char and (time.perf_counter() - last_rx_time > 2.0):
                    logger.debug("[UART] Console silent. Injecting ping to redraw prompt...")
                    self.serial.raw_write(active_ping_char)
                    last_rx_time = time.perf_counter()
                time.sleep(0.01)
                continue

            last_rx_time = time.perf_counter()
            self.serial.parser.ingest(chunk)
            clean = self.serial.parser.buffer.encode('utf-8')
            elapsed = round(time.perf_counter() - t_start, 3)

            # 1. Panic detection (always fatal — terminates the stream)
            if self.panic_pattern.search(clean):
                yield PanicDetected(
                    elapsed_s=elapsed,
                    raw_output=clean[-500:].decode('utf-8', errors='ignore'),
                )
                return

            # 2. Autoboot window detection (yields once)
            if autoboot_trigger and not autoboot_fired and autoboot_trigger in clean:
                autoboot_fired = True
                yield AutobootWindowDetected(elapsed_s=elapsed)

            # 3. Prompt detection
            for ptype, pbytes in prompts.items():
                if pbytes in clean:
                    logger.debug(f"[UART-FSM] Detected prompt {ptype} in buffer!")
                    yield PromptDetected(elapsed_s=elapsed, prompt_type=ptype)

            # 4. Line-level processing (milestones + debug logging)
            for line in self.serial.parser.extract_lines():
                yield BootDataReceived(elapsed_s=elapsed, line=line.strip())
                found_keys = [k for k, v in pending_milestones.items() if v in line]
                for k in found_keys:
                    pending_milestones.pop(k)
                    yield MilestoneReached(elapsed_s=elapsed, name=k)

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
        Asynchronous variant of the UART event stream.
        """
        import anyio
        t_start = time.perf_counter()
        pending_milestones = dict(milestones) if milestones else {}
        autoboot_fired = False

        if flush:
            await self.serial.async_flush_buffers()

        last_rx_time = time.perf_counter()

        while time.perf_counter() - t_start < timeout_s:
            chunk = await self.serial.async_raw_read_chunk()
            if not chunk:
                if active_ping_char and (time.perf_counter() - last_rx_time > 2.0):
                    logger.debug("[UART] Console silent. Injecting ping to redraw prompt...")
                    await self.serial.async_raw_write(active_ping_char)
                    last_rx_time = time.perf_counter()
                await anyio.sleep(0.01)
                continue

            last_rx_time = time.perf_counter()
            self.serial.parser.ingest(chunk)
            clean = self.serial.parser.buffer.encode('utf-8')
            elapsed = round(time.perf_counter() - t_start, 3)

            if self.panic_pattern.search(clean):
                yield PanicDetected(
                    elapsed_s=elapsed,
                    raw_output=clean[-500:].decode('utf-8', errors='ignore'),
                )
                return

            if autoboot_trigger and not autoboot_fired and autoboot_trigger in clean:
                autoboot_fired = True
                yield AutobootWindowDetected(elapsed_s=elapsed)

            for ptype, pbytes in prompts.items():
                if pbytes in clean:
                    logger.debug(f"[UART-FSM] Detected prompt {ptype} in buffer!")
                    yield PromptDetected(elapsed_s=elapsed, prompt_type=ptype)

            for line in self.serial.parser.extract_lines():
                yield BootDataReceived(elapsed_s=elapsed, line=line.strip())
                found_keys = [k for k, v in pending_milestones.items() if v in line]
                for k in found_keys:
                    pending_milestones.pop(k)
                    yield MilestoneReached(elapsed_s=elapsed, name=k)

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
    def cold_boot_to_bootloader(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        """From POWER_OFF → BOOTLOADER. Board must be freshly energized."""
        ...

    @abstractmethod
    def cold_boot_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        """From POWER_OFF → OS_USERLAND. Full boot sequence."""
        ...

    @abstractmethod
    def resume_bootloader_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        """From BOOTLOADER → OS_USERLAND. Board is already at U-Boot."""
        ...

    @abstractmethod
    async def async_cold_boot_to_bootloader(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        ...

    @abstractmethod
    async def async_cold_boot_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        ...

    @abstractmethod
    async def async_resume_bootloader_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        ...


class AutobootStrategy(BootStrategy):
    """
    For boards with U-Boot autoboot countdown enabled.

    Intercepts the autoboot window by spamming interrupt characters,
    giving the FSM deterministic control over the boot process.
    """

    def cold_boot_to_bootloader(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        fsm._do_energize()
        fsm._event_wait_for_bootloader(intercept_autoboot=True)

    def cold_boot_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        fsm._do_energize()
        fsm._event_wait_for_bootloader(intercept_autoboot=True)
        fsm._event_boot_from_bootloader_to_os()

    def resume_bootloader_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        fsm._event_boot_from_bootloader_to_os()

    async def async_cold_boot_to_bootloader(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._do_energize)
        await fsm.async_event_wait_for_bootloader(intercept_autoboot=True)

    async def async_cold_boot_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._do_energize)
        await fsm.async_event_wait_for_bootloader(intercept_autoboot=True)
        await fsm.async_event_boot_from_bootloader_to_os()

    async def async_resume_bootloader_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        await fsm.async_event_boot_from_bootloader_to_os()


class TrapRebootStrategy(BootStrategy):
    """
    For boards with autoboot disabled (e.g., Secure Boot / HAB-locked).

    Cannot intercept U-Boot during normal boot. Instead, boots all the way
    to Linux, sets an fw_setenv trap, reboots, and catches U-Boot on the
    way back down.
    """

    def cold_boot_to_bootloader(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        fsm._do_energize()
        fsm._event_wait_for_os_shell()
        fsm._finalize_os_boot()
        fsm._set_uboot_trap_and_reboot()
        fsm._event_wait_for_bootloader(intercept_autoboot=False)

    def cold_boot_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        fsm._do_energize()
        fsm._event_wait_for_os_shell()

    def resume_bootloader_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        fsm._restore_uboot_trap()
        fsm._event_boot_from_bootloader_to_os()

    async def async_cold_boot_to_bootloader(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._do_energize)
        await fsm.async_event_wait_for_os_shell()
        await anyio.to_thread.run_sync(fsm._finalize_os_boot)
        await anyio.to_thread.run_sync(fsm._set_uboot_trap_and_reboot)
        await fsm.async_event_wait_for_bootloader(intercept_autoboot=False)

    async def async_cold_boot_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._do_energize)
        await fsm.async_event_wait_for_os_shell()

    async def async_resume_bootloader_to_os(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        import anyio
        await anyio.to_thread.run_sync(fsm._restore_uboot_trap)
        await fsm.async_event_boot_from_bootloader_to_os()


class RecoveryStrategy(ABC):
    """
    Encapsulates how to force the hardware into a low-level USB/Serial recovery mode
    (e.g., NXP Serial Downloader, STM32 DFU, TI UART boot).
    """
    @abstractmethod
    def trigger_recovery(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        ...


class GpioRecoveryStrategy(RecoveryStrategy):
    """
    Forces recovery mode by asserting a physical GPIO pin (e.g., pulling BOOT_MODE high)
    while power cycling the board.
    """
    def trigger_recovery(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        recovery_pin = getattr(fsm.cfg, "gpio_recovery_pin", "RECOVERY_BTN")
        if fsm.gpio and recovery_pin:
            fsm.gpio.set_pin(recovery_pin, True)
            fsm._do_energize()
            time.sleep(getattr(fsm.cfg, "recovery_latch_time_s", 1.5))
            fsm.gpio.set_pin(recovery_pin, False)
        else:
            logger.warning("manual_action_required", instructions="PRESS AND HOLD THE RECOVERY BUTTON / SET JUMPER NOW.")
            try: input(">>> Press [ENTER] while holding the button... ")
            except EOFError: pass

            fsm._do_energize()
            logger.warning("manual_action_required", instructions="POWER IS ON. YOU CAN NOW RELEASE THE RECOVERY BUTTON.")


class ContextValidationStrategy(ABC):
    """
    Encapsulates OS-level validation (e.g., SWUpdate A/B partition checks, EVSE calibration).
    """
    @abstractmethod
    def verify_linux_context(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        ...


class DefaultSWUpdateStrategy(ContextValidationStrategy):
    """
    Default OS validation strategy that parses the SWUpdate IPC socket to determine
    the active RootFS partition.
    """
    def verify_linux_context(self, fsm: 'EmbeddedLinuxStateMachine') -> None:
        logger.info("validating_ab_partitions_via_swupdate")
        res_sw = fsm.serial.safe_run("swupdate -g", timeout_s=3.0, check_exit_code=False)

        if res_sw.ok:
            output = res_sw.stdout.strip()
            shell_prompt = getattr(fsm.cfg, "os_shell_prompt", "~#")
            lines = [l.strip() for l in output.split('\n') if l.strip() and "swupdate" not in l and shell_prompt not in l]
            fsm.context.active_rootfs = lines[-1] if lines else "UNKNOWN"
        else:
            logger.debug("swupdate_not_found_or_failed", action="setting_rootfs_to_unknown")
            fsm.context.active_rootfs = "UNKNOWN"

        crypto_part = getattr(fsm.cfg, 'storage_data_encrypted', '/dev/mapper/data_crypt')
        if crypto_part:
            mount_res = fsm.serial.safe_run("mount | grep /data", timeout_s=3.0, check_exit_code=False)
            fsm.context.crypto_data_mounted = crypto_part in mount_res.stdout


class BaseDutStateMachine(ABC):
    STATES = [DutState.POWER_OFF, DutState.ENERGIZED, DutState.BOOTLOADER, DutState.OS_USERLAND, DutState.RECOVERY, DutState.DIRTY]
    PANIC_WATCHDOG: Pattern[bytes] = re.compile(br"(Kernel panic - not syncing|Out of memory: Killed process|synchronous external abort|HAB Events|SEC_ERR|Signature Verification Failed)")
    ANSI_ESCAPE_B: Pattern[bytes] = re.compile(br'\x1b\[[0-9;]*[a-zA-Z]')
    # Use 'Any' here so type checkers don't yell when a project uses its own Enum
    state: Any

    def __init__(
        self,
        psu: Optional[ScpiPowerSupply],
        serial: EphemeralSerialClient,
        ssh: EphemeralSSHClient,
        cfg: StateMachineConfig,
        boot_profiler_cfg: Optional[BootProfilerConfig] = None,
        gpio: Optional[Any] = None
    ):
        """Initializes the core Hardware State Machine context.

        Args:
            psu: Programmable power supply for cold-booting, if available.
            serial: UART transport used for early bootloader monitoring.
            ssh: High-speed Ethernet transport for OS-level interactions.
            cfg: Top-level TOML configuration parameters.
            boot_profiler_cfg: Analytics configuration for tracking boot phase duration.
            gpio: Optional hardware fixture controller for physical interaction.
        """
        self.psu = psu
        self.serial = serial
        self.ssh = ssh
        self.cfg = cfg
        self.boot_profiler_cfg = boot_profiler_cfg
        self.gpio = gpio

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

        self.machine = Machine(
            model=self,
            states=self.STATES,
            initial=DutState.DIRTY,
            send_event=True,
            title="MES Hardware State Graph",
            show_conditions=True
        )

        self.machine.add_transition('power_off', '*', DutState.POWER_OFF, before='_hw_power_off')
        self.machine.add_transition('energize', '*', DutState.ENERGIZED, before='_hw_energize')
        self.machine.add_transition('boot_to_bootloader', '*', DutState.BOOTLOADER, before='_hw_boot_to_bootloader')
        self.machine.add_transition('boot_to_os', '*', DutState.OS_USERLAND, before='_hw_boot_to_os')
        self.machine.add_transition('boot_to_recovery', '*', DutState.RECOVERY, before='_hw_to_recovery')
        self.machine.add_transition('mark_dirty', '*', DutState.DIRTY, before=lambda e: logger.warning("state_marked_dirty"))

        self._register_custom_states()

    def _register_custom_states(self) -> None:
        """Override this in project subclasses to add custom states andtransitions.
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

    @abstractmethod
    def _hw_power_off(self, event: EventData) -> None: pass
    @abstractmethod
    def _hw_energize(self, event: EventData) -> None: pass
    @abstractmethod
    def _hw_boot_to_bootloader(self, event: EventData) -> None: pass
    @abstractmethod
    def _hw_boot_to_os(self, event: EventData) -> None: pass
    @abstractmethod
    def _hw_to_recovery(self, event: EventData) -> None: pass


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
                power_threshold = getattr(self.cfg, "power_off_threshold_a", 0.05)
                
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
            # Board is actively streaming logs. It's fully powered and transitioning.
            logger.debug("probe_result", domain="serial", bytes_received=len(passive_chunk), state="ENERGIZED")
            return DutState.ENERGIZED

        self.serial.flush_buffers()
        self.serial.raw_write(b"\r\n")
        time.sleep(0.4)

        resp = bytearray()
        while True:
            chunk = self.serial.raw_read_chunk()
            if not chunk:
                break
            resp.extend(chunk)
            time.sleep(0.05)

        # RETRY PASS: an idle login prompt (agetty) can take 1-3s to respond
        # to an empty newline. Also, UART chips often swallow the first byte 
        # after port initialization. If the fast pass returned nothing, ping
        # again and wait longer before declaring POWER_OFF.
        if not resp:
            logger.debug("probe_fast_pass_silent", action="injecting_ping_and_retrying")
            self.serial.raw_write(b"\r\n")
            resp = bytearray()
            while True:
                chunk = self.serial.raw_read_chunk()
                if not chunk:
                    break
                resp.extend(chunk)
                time.sleep(0.05)

            # RETRY PASS: an idle login prompt (agetty) can take 1-3s to respond
            # to an empty newline. Also, UART chips often swallow the first byte 
            # after port initialization. If the fast pass returned nothing, ping
            # again and wait longer before declaring POWER_OFF.
            if not resp:
                logger.debug("probe_fast_pass_silent", action="injecting_ping_and_retrying")
                self.serial.raw_write(b"\r\n")
                time.sleep(2.0)
                while True:
                    chunk = self.serial.raw_read_chunk()
                    if not chunk:
                        break
                    resp.extend(chunk)
                    time.sleep(0.05)

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
        - ``force`` is False AND the state is RECOVERY or BOOTLOADER
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
        if medium == "default" or not medium: medium = "default"
        straps = getattr(self.cfg, "boot_straps_gpio_map", {}).get(medium)

        if self.gpio:
            if straps:
                for pin, state in straps.items(): self.gpio.set_pin(pin, state)
                time.sleep(0.5)
            return

        if not self.gpio or (medium != "default" and not straps):
            logger.warning("="*60)
            if medium == "default":
                logger.warning("[MANUAL ACTION] RESTORE HARDWARE BOOT STRAP PINS / DIP SWITCHES TO DEFAULT.")
            else:
                logger.warning(f"[MANUAL ACTION] SET HARDWARE BOOT STRAP PINS TO: **{medium.upper()}**")
            logger.warning("="*60)
            try: input(">>> Press [ENTER] once configured... ")
            except EOFError: time.sleep(2.0)

    def _do_hardware_reset(self) -> None:
        """Toggles the physical RESET pin on the board via GPIO.

        If no reset pin is defined, falls back to a hard power cycle.
        """
        reset_pin = getattr(self.cfg, "gpio_reset_pin", None)
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
            logger.warning("manual_action_required", instructions="UNPLUG THE 12V POWER FROM THE BOARD NOW.")
            try: input(">>> Press [ENTER] once powered off... ")
            except EOFError: time.sleep(2.0)

    def _do_energize(self) -> None:
        """Applies physical voltage to the board and captures inrush current.

        If no PSU is connected, pauses FSM execution and prompts the user
        to manually plug in the power cable.
        """
        logger.info("applying_raw_power", medium=self.context.active_boot_medium)

        if self.psu:
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
            logger.warning("manual_action_required", instructions="PLUG IN THE 12V POWER NOW.")
            try: input(">>> Press [ENTER] once power is applied... ")
            except EOFError: time.sleep(2.0)

        # Securely re-bind the serial port to recover the file descriptor.
        # If the USB-Serial adapter is physically on the board, it drops and re-enumerates during a power cycle.
        self._rebind_serial_port()

    @retry(stop=stop_after_attempt(50), wait=wait_fixed(0.1), reraise=True, before_sleep=before_sleep_log(logger, logging.DEBUG))
    def _rebind_serial_port(self) -> None:
        """Attempts to reconnect the serial port after a hardware power cycle."""
        self.serial.disconnect()
        self.serial.connect()

    def _set_uboot_trap_and_reboot(self) -> None:
        logger.info("hot_patching_uboot_env")
        # Robustness: We MUST check exit codes here. Silently failing fw_setenv will brick the FSM trap loop.
        self.serial.safe_run("fw_setenv mes_prev_bootcmd \"$(fw_printenv -n bootcmd)\"", timeout_s=3.0, check_exit_code=True)
        self.serial.safe_run("fw_setenv bootcmd 'echo MES Framework Trap'", timeout_s=3.0, check_exit_code=True)
        self.serial.safe_run("reboot", timeout_s=2.0, check_exit_code=False)

    def _restore_uboot_trap(self) -> None:
        logger.info("restoring_original_uboot_env")
        prompt = self.cfg.bootloader_prompt
        self.serial.safe_run('setenv bootcmd "${mes_prev_bootcmd}"', expected_prompt=prompt, timeout_s=3.0)
        self.serial.safe_run('setenv mes_prev_bootcmd', expected_prompt=prompt, timeout_s=3.0)
        self.serial.safe_run('saveenv', expected_prompt=prompt, timeout_s=5.0)

    # =========================================================================
    # EVENT-DRIVEN BOOT METHODS
    # =========================================================================

    def _event_wait_for_bootloader(self, intercept_autoboot: bool) -> None:
        """Event-driven bootloader interception.

        Consumes events from the UartEventStream until a bootloader prompt
        is detected, then synchronizes with an echo command.

        Args:
            intercept_autoboot: If True, fires interrupt characters to stop autoboot.
        """
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

        for event in self.event_stream.open(
            prompts=prompts,
            timeout_s=self.cfg.cold_boot_timeout_s,
            autoboot_trigger=autoboot_trigger,
            active_ping_char=ping_char,
        ):
            if isinstance(event, PanicDetected):
                raise KernelPanicError(
                    f"Kernel panic during Bootloader routing:\n{event.raw_output}"
                )

            elif isinstance(event, AutobootWindowDetected):
                logger.info("autoboot_window_detected_sniping")
                for _ in range(3):
                    self.serial.raw_write(blast_bytes)
                    time.sleep(0.05)

            elif isinstance(event, PromptDetected) and event.prompt_type in ("bootloader", "trap"):
                # Synchronize with the prompt via echo
                self.serial.raw_write(b"\n")
                time.sleep(0.1)
                self.serial.flush_buffers()

                res = self.serial.safe_run(
                    "echo MES_SYNC",
                    expected_prompt=self.cfg.bootloader_prompt,
                    timeout_s=3.0,
                )
                if "MES_SYNC" not in res.stdout:
                    raise BootloaderSyncError(
                        "Failed to synchronize with Bootloader prompt after detection."
                    )
                logger.info("bootloader_intercepted_successfully")
                return

            elif isinstance(event, BootDataReceived):
                logger.debug("uart_rx", data=event.line)

        raise BootloaderTimeoutError(
            f"Failed to intercept Bootloader within {self.cfg.cold_boot_timeout_s}s timeout."
        )

    def _event_boot_from_bootloader_to_os(self) -> None:
        """Send the boot command from U-Boot and wait for OS shell via event stream."""
        logger.info("commanding_os_boot", boot_cmd=self.cfg.bootloader_boot_cmd)
        self.serial.flush_buffers()
        self.serial.raw_write(f"{self.cfg.bootloader_boot_cmd}\n".encode())
        self._event_wait_for_os_shell(flush=False)

    async def async_event_boot_from_bootloader_to_os(self) -> None:
        """Send the boot command from U-Boot and wait for OS shell via async event stream."""
        logger.info("commanding_os_boot", boot_cmd=self.cfg.bootloader_boot_cmd)
        import anyio
        await self.serial.async_flush_buffers()
        await self.serial.async_raw_write(f"{self.cfg.bootloader_boot_cmd}\n".encode())
        await self.async_event_wait_for_os_shell(flush=False)

    def _event_wait_for_os_shell(self, flush: bool = True) -> None:
        """Event-driven OS boot monitor.

        Waits for the Linux shell prompt, handling login/password prompts
        and recording boot profiler milestones along the way.

        Args:
            flush: Whether to flush the UART buffer before reading.
        """
        logger.info("waiting_for_linux_userland")
        self.boot_metrics.clear()

        prompts: Dict[str, bytes] = {
            "shell": self.cfg.os_shell_prompt.encode('utf-8'),
            "login": self.cfg.os_login_prompt.encode('utf-8'),
        }
        if self.cfg.os_password:
            prompts["password"] = self.cfg.os_password_prompt.encode('utf-8')

        milestones = (
            self.boot_profiler_cfg.milestones.copy()
            if self.boot_profiler_cfg else {}
        )

        for event in self.event_stream.open(
            prompts=prompts,
            timeout_s=self.cfg.cold_boot_timeout_s,
            milestones=milestones,
            flush=flush,
            active_ping_char=b"\n",
        ):
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
                    time.sleep(0.1)
                    self.serial.write_line(self.cfg.os_user or "root")
                    self.serial.parser.clear_buffer()

                elif event.prompt_type == "password":
                    time.sleep(0.1)
                    self.serial.write_line(self.cfg.get_os_password() or "", sensitive=True)

        raise TransportTimeoutError("Timed out waiting for Linux Shell prompt.")

    async def async_event_wait_for_bootloader(self, intercept_autoboot: bool) -> None:
        """Asynchronous event-driven bootloader interception."""
        logger.info("hunting_for_bootloader_prompt")
        import anyio

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

        async for event in self.event_stream.open_async(
            prompts=prompts,
            timeout_s=self.cfg.cold_boot_timeout_s,
            autoboot_trigger=autoboot_trigger,
            active_ping_char=ping_char,
        ):
            if isinstance(event, PanicDetected):
                raise KernelPanicError(
                    f"Kernel panic during Bootloader routing:\n{event.raw_output}"
                )

            elif isinstance(event, AutobootWindowDetected):
                logger.info("autoboot_window_detected_sniping")
                for _ in range(3):
                    await self.serial.async_raw_write(blast_bytes)
                    await anyio.sleep(0.05)

            elif isinstance(event, PromptDetected) and event.prompt_type in ("bootloader", "trap"):
                await self.serial.async_raw_write(b"\n")
                await anyio.sleep(0.1)
                await self.serial.async_flush_buffers()

                from functools import partial
                res = await anyio.to_thread.run_sync(
                    partial(self.serial.safe_run, "echo MES_SYNC", expected_prompt=self.cfg.bootloader_prompt, timeout_s=3.0)
                )

                if "MES_SYNC" not in res.stdout:
                    raise BootloaderSyncError("Failed to synchronize with Bootloader prompt after detection.")
                logger.info("bootloader_intercepted_successfully")
                return

            elif isinstance(event, BootDataReceived):
                logger.debug("uart_rx", data=event.line)

        raise BootloaderTimeoutError(
            f"Failed to intercept Bootloader within {self.cfg.cold_boot_timeout_s}s timeout."
        )

    async def async_event_wait_for_os_shell(self, flush: bool = True) -> None:
        """Asynchronous event-driven OS boot monitor."""
        logger.info("waiting_for_linux_userland")
        self.boot_metrics.clear()
        import anyio

        prompts: Dict[str, bytes] = {
            "shell": self.cfg.os_shell_prompt.encode('utf-8'),
            "login": self.cfg.os_login_prompt.encode('utf-8'),
        }
        if self.cfg.os_password:
            prompts["password"] = self.cfg.os_password_prompt.encode('utf-8')

        milestones = (
            self.boot_profiler_cfg.milestones.copy()
            if self.boot_profiler_cfg else {}
        )

        async for event in self.event_stream.open_async(
            prompts=prompts,
            timeout_s=self.cfg.cold_boot_timeout_s,
            milestones=milestones,
            flush=flush,
            active_ping_char=b"\n",
        ):
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
                    await anyio.to_thread.run_sync(self.serial.write_line, self.cfg.get_os_password() or "", True)
                    self.serial.parser.clear_buffer()

            elif isinstance(event, BootDataReceived):
                logger.debug("uart_rx", data=event.line)

        raise TransportTimeoutError("Timed out waiting for Linux Shell prompt.")

    # Backward-compatible aliases for any external code referencing old methods
    def _do_wait_for_bootloader(self, spam_interrupt: bool) -> None:
        self._event_wait_for_bootloader(intercept_autoboot=spam_interrupt)

    def _do_boot_from_bootloader_to_os(self) -> None:
        self._event_boot_from_bootloader_to_os()

    def _do_wait_for_os(self) -> None:
        self._event_wait_for_os_shell()


    def _finalize_os_boot(self) -> None:
        """Executes final OS verification and bridges the SSH transport.

        Relies on the Yocto image to have pre-baked SSH keys or default passwords.
        Harvests systemd boot times and establishes the primary SSH connection
        once the OS is fully validated.
        """
        if self.ssh.is_connected:
            return

        # 1. Parse SWUpdate or Custom Validators
        self._verify_linux_context()

        # 1.5. Harvest Kernel Boot Analytics
        res_sysd = self.serial.safe_run("systemd-analyze time", timeout_s=5.0, check_exit_code=False)
        if res_sysd.ok and "Startup finished in" in res_sysd.stdout:
            try:
                k_match = re.search(r'([\d\.]+)s\s*\(kernel\)', res_sysd.stdout)
                u_match = re.search(r'([\d\.]+)s\s*\(userspace\)', res_sysd.stdout)
                if k_match: self.boot_metrics["t_systemd_kernel_s"] = float(k_match.group(1))
                if u_match: self.boot_metrics["t_systemd_userspace_s"] = float(u_match.group(1))
            except Exception:
                pass

        # 2. Establish the high-speed SSH pipeline
        logger.info("establishing_primary_ssh_transport")
        self.ssh.connect()

    async def async_finalize_os_boot(self) -> None:
        """Async variant of finalize_os_boot."""
        if self.ssh.is_connected:
            return
        import anyio
        await anyio.to_thread.run_sync(self._verify_linux_context)

        res_sysd = await self.serial.async_safe_run("systemd-analyze time", timeout_s=5.0, check_exit_code=False)
        if res_sysd.ok and "Startup finished in" in res_sysd.stdout:
            try:
                k_match = re.search(r'([\d\.]+)s\s*\(kernel\)', res_sysd.stdout)
                u_match = re.search(r'([\d\.]+)s\s*\(userspace\)', res_sysd.stdout)
                if k_match: self.boot_metrics["t_systemd_kernel_s"] = float(k_match.group(1))
                if u_match: self.boot_metrics["t_systemd_userspace_s"] = float(u_match.group(1))
            except Exception:
                pass

        logger.info("establishing_primary_ssh_transport")
        await self.ssh.async_connect()

    def verify_heartbeat(self) -> bool:
        """Ping the OS state via UART heartbeat to verify it's still alive.

        Returns:
            bool: True if the device successfully echoes the heartbeat payload, False otherwise.
        """
        if not self.serial.is_connected:
            return False
        logger.debug("verifying_uart_heartbeat")
        res = self.serial.safe_run("echo MES_HEARTBEAT", timeout_s=2.0, check_exit_code=False)
        return "MES_HEARTBEAT" in res.stdout

    # =========================================================================
    # SOTA CONTEXT VALIDATION
    # =========================================================================

    def _verify_linux_context(self) -> None:
        """Executes dynamically injected Validators, or defaults to the selected Context Strategy.

        Parses the current A/B partition configuration and populates
        the DeviceContext. If custom validators are registered, it executes them
        sequentially instead.
        """
        if self.context_validators:
            logger.info("executing_dynamic_context_validators", count=len(self.context_validators))
            for validator in self.context_validators:
                try:
                    validator(self)
                except Exception as e:
                    logger.error("custom_context_validator_failed", error=str(e))
            return

        self.context_strategy.verify_linux_context(self)

    # =========================================================================
    # FSM 'BEFORE' TRANSITION HOOKS
    # =========================================================================

    def _hw_power_off(self, event: EventData) -> None:
        self._align_to_physical_state()
        if self.state == DutState.POWER_OFF: return
        self._do_power_off()

    def _hw_energize(self, event: EventData) -> None:
        self._align_to_physical_state()
        target_medium = event.kwargs.get("medium", self.context.active_boot_medium)
        needs_strap_change = target_medium != self.context.active_boot_medium

        if needs_strap_change or self.state == DutState.RECOVERY:
            self._do_power_off()
            self._do_apply_bootstrap(target_medium)
            self.context.active_boot_medium = target_medium
            self._do_energize()
            return

        if self.state in [DutState.ENERGIZED, DutState.BOOTLOADER, DutState.OS_USERLAND]: return
        self._do_apply_bootstrap(target_medium)
        self.context.active_boot_medium = target_medium
        self._do_energize()

    def _hw_boot_to_bootloader(self, event: EventData) -> None:
        self._align_to_physical_state()
        target_medium = event.kwargs.get("medium", self.context.active_boot_medium)
        needs_strap_change = target_medium != self.context.active_boot_medium

        # Path A: Medium change or recovery — always cold boot via strategy
        if needs_strap_change or self.state == DutState.RECOVERY:
            self._do_power_off()
            self._do_apply_bootstrap(target_medium)
            self.context.active_boot_medium = target_medium
            self.boot_strategy.cold_boot_to_bootloader(self)
            return

        # Short-circuit: already at bootloader
        if self.state == DutState.BOOTLOADER:
            return

        # Path B: Need to get to bootloader from current state
        if self.state == DutState.OS_USERLAND:
            self.serial.safe_run("reboot", timeout_s=2.0, check_exit_code=False)
        elif self.state == DutState.ENERGIZED:
            if not self.psu:
                self._do_soft_reboot()
            else:
                self._do_power_off()
                self.boot_strategy.cold_boot_to_bootloader(self)
                return
        else:
            self.boot_strategy.cold_boot_to_bootloader(self)
            return

        # Board is rebooting — catch bootloader on the way back
        self._event_wait_for_bootloader(intercept_autoboot=self.cfg.autoboot_enabled)

    async def async_hw_boot_to_bootloader(self, medium: str = None) -> None:
        """Asynchronous, manual bypass for boot_to_bootloader."""
        import anyio
        await anyio.to_thread.run_sync(self._align_to_physical_state)
        target_medium = medium or self.context.active_boot_medium
        needs_strap_change = target_medium != self.context.active_boot_medium

        # Path A: Medium change or recovery — always cold boot via strategy
        if needs_strap_change or self.state == DutState.RECOVERY:
            await anyio.to_thread.run_sync(self._do_power_off)
            await anyio.to_thread.run_sync(self._do_apply_bootstrap, target_medium)
            self.context.active_boot_medium = target_medium
            await self.boot_strategy.async_cold_boot_to_bootloader(self)
            await anyio.to_thread.run_sync(self.machine.set_state, DutState.BOOTLOADER)
            return

        # Short-circuit: already at bootloader
        if self.state == DutState.BOOTLOADER:
            return

        # Path B: Need to get to bootloader from current state
        if self.state == DutState.OS_USERLAND:
            await self.serial.async_safe_run("reboot", timeout_s=2.0, check_exit_code=False)
        elif self.state == DutState.ENERGIZED:
            if not self.psu:
                await anyio.to_thread.run_sync(self._do_soft_reboot)
            else:
                await anyio.to_thread.run_sync(self._do_power_off)
                await self.boot_strategy.async_cold_boot_to_bootloader(self)
                await anyio.to_thread.run_sync(self.machine.set_state, DutState.BOOTLOADER)
                return
        else:
            await self.boot_strategy.async_cold_boot_to_bootloader(self)
            await anyio.to_thread.run_sync(self.machine.set_state, DutState.BOOTLOADER)
            return

        # Board is rebooting — catch bootloader on the way back
        await self.async_event_wait_for_bootloader(intercept_autoboot=self.cfg.autoboot_enabled)
        await anyio.to_thread.run_sync(self.machine.set_state, DutState.BOOTLOADER)

    def _hw_boot_to_os(self, event: EventData) -> None:
        self.boot_metrics.clear()
        self._align_to_physical_state()

        target_medium = event.kwargs.get("medium", self.context.active_boot_medium)
        needs_strap_change = target_medium != self.context.active_boot_medium

        if needs_strap_change:
            logger.info("boot_medium_change_requested", target_medium=target_medium, action="forcing_hard_reboot")
            self.machine.set_state(DutState.DIRTY)

        # --- TRAP 1: Resume existing OS (zombie detection) ---
        if self.state == DutState.OS_USERLAND:
            if self._try_resume_existing_os():
                return
            self.machine.set_state(DutState.DIRTY)
            self._do_hardware_reset()

        # --- TRAP 2: Hot-login from ENERGIZED ---
        if self.state == DutState.ENERGIZED:
            if self._try_hot_login():
                return
            self.machine.set_state(DutState.DIRTY)
            self._do_hardware_reset()

        # --- TRAP 3: Cold boot via strategy ---
        if self.state in [DutState.RECOVERY, DutState.DIRTY, DutState.POWER_OFF]:
            if self.state != DutState.POWER_OFF:
                self._do_power_off()
            self._do_apply_bootstrap(target_medium)
            self.context.active_boot_medium = target_medium
            self.boot_strategy.cold_boot_to_os(self)
            self._finalize_os_boot()
            return

        # --- TRAP 4: Resume from bootloader via strategy ---
        if self.state == DutState.BOOTLOADER:
            self.boot_strategy.resume_bootloader_to_os(self)
            self._finalize_os_boot()
            return

    async def async_hw_boot_to_os(self, medium: str = None) -> None:
        """Asynchronous, manual bypass for boot_to_os. Allows event loop integration."""
        self.boot_metrics.clear()
        import anyio
        await anyio.to_thread.run_sync(self._align_to_physical_state)

        target_medium = medium or self.context.active_boot_medium
        needs_strap_change = target_medium != self.context.active_boot_medium

        if needs_strap_change:
            logger.info("boot_medium_change_requested", target_medium=target_medium, action="forcing_hard_reboot")
            await anyio.to_thread.run_sync(self.machine.set_state, DutState.DIRTY)

        # --- TRAP 1: Resume existing OS (zombie detection) ---
        if self.state == DutState.OS_USERLAND:
            if await self.async_try_resume_existing_os():
                await anyio.to_thread.run_sync(self.machine.set_state, DutState.OS_USERLAND)
                return
            await anyio.to_thread.run_sync(self.machine.set_state, DutState.DIRTY)
            await anyio.to_thread.run_sync(self._do_hardware_reset)

        # --- TRAP 2: Hot-login from ENERGIZED ---
        if self.state == DutState.ENERGIZED:
            if await self.async_try_hot_login():
                await anyio.to_thread.run_sync(self.machine.set_state, DutState.OS_USERLAND)
                return
            await anyio.to_thread.run_sync(self.machine.set_state, DutState.DIRTY)
            await anyio.to_thread.run_sync(self._do_hardware_reset)

        # --- TRAP 3: Cold boot via strategy ---
        if self.state in [DutState.RECOVERY, DutState.DIRTY, DutState.POWER_OFF]:
            if self.state != DutState.POWER_OFF:
                await anyio.to_thread.run_sync(self._do_power_off)
            await anyio.to_thread.run_sync(self._do_apply_bootstrap, target_medium)
            self.context.active_boot_medium = target_medium
            await self.boot_strategy.async_cold_boot_to_os(self)
            await self.async_finalize_os_boot()
            await anyio.to_thread.run_sync(self.machine.set_state, DutState.OS_USERLAND)
            return

        # --- TRAP 4: Resume from bootloader via strategy ---
        if self.state == DutState.BOOTLOADER:
            await self.boot_strategy.async_resume_bootloader_to_os(self)
            await self.async_finalize_os_boot()
            await anyio.to_thread.run_sync(self.machine.set_state, DutState.OS_USERLAND)
            return

    def _try_resume_existing_os(self) -> bool:
        """Attempt to reuse an existing OS_USERLAND session. Returns True on success."""
        logger.debug("verifying_uart_heartbeat_existing_os")
        res = self.serial.safe_run("echo MES_HEARTBEAT", timeout_s=2.0, check_exit_code=False)
        if "MES_HEARTBEAT" in res.stdout:
            try:
                self._finalize_os_boot()
                return True
            except TransportConnectionError:
                logger.warning("ssh_provision_failed_on_existing_os", action="marking_dirty")
        else:
            logger.warning("uart_heartbeat_failed", reason="os_is_a_zombie", action="marking_dirty")
        return False

    async def async_try_resume_existing_os(self) -> bool:
        """Async attempt to reuse an existing OS_USERLAND session. Returns True on success."""
        logger.debug("verifying_uart_heartbeat_existing_os")
        res = await self.serial.async_safe_run("echo MES_HEARTBEAT", timeout_s=2.0, check_exit_code=False)
        if "MES_HEARTBEAT" in res.stdout:
            try:
                await self.async_finalize_os_boot()
                return True
            except TransportConnectionError:
                logger.warning("ssh_provision_failed_on_existing_os", action="marking_dirty")
        else:
            logger.warning("uart_heartbeat_failed", reason="os_is_a_zombie", action="marking_dirty")
        return False

    def _try_hot_login(self) -> bool:
        """
        Attempt hot-login from ENERGIZED (Actively Booting or at Login) state.

        Delegates to _event_wait_for_os_shell to robustly handle both active 
        streaming and idle login prompts.
        """
        logger.info("hot_login_from_energized")

        try:
            # Robustness: We no longer blindly write the username because ENERGIZED 
            # now includes active log streaming. _event_wait_for_os_shell has a built-in
            # ping to redraw the prompt if idle, and reliably catches the login prompt.
            self._event_wait_for_os_shell(flush=False)
            self._finalize_os_boot()
            return True
        except TransportTimeoutError:
            logger.warning("hot_login_failed", reason="shell_prompt_not_reached", action="marking_dirty")
            return False

    async def async_try_hot_login(self) -> bool:
        """Async variant of try_hot_login."""
        logger.info("hot_login_from_energized")
        try:
            await self.async_event_wait_for_os_shell(flush=False)
            await self.async_finalize_os_boot()
            return True
        except TransportTimeoutError:
            logger.warning("hot_login_failed", reason="shell_prompt_not_reached", action="marking_dirty")
            return False

    def _hw_to_recovery(self, event: EventData) -> None:
        self._align_to_physical_state()
        if self.state == DutState.RECOVERY: return

        logger.info("routing_to_hardware_recovery", action="power_cycle_required")
        if self.state != DutState.POWER_OFF: self._do_power_off()

        self.recovery_strategy.trigger_recovery(self)
        time.sleep(2.0)
