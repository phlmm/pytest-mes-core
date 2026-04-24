import time
import re
import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional, Pattern, Any, Callable, List, Generator
from dataclasses import dataclass, field
from transitions import Machine, EventData
from enum import Enum, auto

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

logger = logging.getLogger("mes_core.state_machine")

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

# ==========================================
# UART EVENT TYPES (Event-Driven Boot)
# ==========================================
@dataclass(frozen=True)
class UartEvent:
    """Base class for all UART events produced during boot monitoring."""
    elapsed_s: float

@dataclass(frozen=True)
class PromptDetected(UartEvent):
    """A known prompt pattern was detected in the UART stream."""
    prompt_type: str  # "shell", "login", "password", "bootloader"

@dataclass(frozen=True)
class AutobootWindowDetected(UartEvent):
    """The U-Boot autoboot countdown message was detected."""
    pass

@dataclass(frozen=True)
class PanicDetected(UartEvent):
    """A kernel panic or secure boot violation was detected."""
    raw_output: str

@dataclass(frozen=True)
class MilestoneReached(UartEvent):
    """A boot profiler milestone string was matched in the UART stream."""
    name: str

@dataclass(frozen=True)
class BootDataReceived(UartEvent):
    """A complete line of boot output was received (for debug logging)."""
    line: str


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

        with self.serial.execution_lock():
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

@dataclass
class DeviceContext:
    active_rootfs: str = "UNKNOWN"
    crypto_data_mounted: bool = False
    active_boot_medium: str = "default"

    manifest: Optional[HardwareManifest] = None

    custom_data: Dict[str, Any] = field(default_factory=dict)

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
        logger.debug(f"[State Machine] Boot Strategy: {type(self.boot_strategy).__name__}")

        logger.debug(f"[State Machine] Initializing FSM. PSU: {self.psu is not None} | GPIO: {self.gpio is not None}")

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
        self.machine.add_transition('mark_dirty', '*', DutState.DIRTY, before=lambda e: logger.warning(f"[State Machine] Marked DIRTY."))

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

    def _probe_uart_for_state(self) -> DutState:
        """Universal UART prober with strict ANSI stripping.

        Single source of truth for physical state detection. Uses a two-pass 
        strategy: a fast 0.4s pass for boards already streaming output, then a 
        2.0s retry pass for boards sitting idle at a login prompt (where agetty 
        may take 1-3s to respond to an empty newline input).

        Returns:
            DutState: The dynamically detected physical state of the target device.
        """
        if not self.serial.is_connected:
            self.serial.connect()
            time.sleep(0.1)

        with self.serial.execution_lock():
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
                logger.debug("[State Machine] Probe: fast pass silent, injecting \\r\\n and retrying with 2.0s wait...")
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

        logger.debug(f"[State Machine] Probe clean response: {clean_resp!r}")
        return DutState.POWER_OFF

    def _do_soft_reboot(self) -> None:
        """
        Consolidates desk-mode soft reboots.
        """
        logger.info("[State Machine] Desk Mode: Attempting soft-login to reboot instead of manual power cycle...")
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
                logger.debug(f"[State Machine] Probe skipped: state is explicitly-set {self.state.name}.")
                return

        logger.debug("[State Machine] Probing UART to align physical state...")
        detected_state = self._probe_uart_for_state()

        state_labels = {
            DutState.OS_USERLAND: "Detected OS Shell",
            DutState.ENERGIZED: "Detected output/login",
            DutState.BOOTLOADER: "Detected Bootloader",
            DutState.POWER_OFF: "Silence",
        }
        logger.info(f"[State Machine] UART Probe: {state_labels.get(detected_state, 'Unknown')}. Aligning to {detected_state.name}.")
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
            logger.warning(f"[State Machine] JTAG/GPIO: Firing physical hardware RESET pin ({reset_pin})...")
            self.gpio.set_pin(reset_pin, True)
            time.sleep(0.5)
            self.gpio.set_pin(reset_pin, False)
            time.sleep(0.5)
        else:
            logger.debug("[State Machine] No hardware reset pin defined. Falling back to hard power cycle.")
            self._do_power_off()
            self._do_energize()

    def _do_power_off(self) -> None:
        """Executes a hard power drop using the PSU.

        If no PSU is connected, pauses FSM execution and prompts the user
        to manually unplug the power cable.
        """
        logger.debug("[State Machine] Executing hard power drop...")
        if self.ssh.is_connected: self.ssh.disconnect()

        cm = self.context.active_boot_medium
        self.context = DeviceContext()
        self.context.active_boot_medium = cm

        if self.psu:
            self.psu.set_voltage(0.0)
            self.psu.disable_output()
            time.sleep(2.0)
        else:
            logger.warning("[MANUAL ACTION] UNPLUG THE 12V POWER FROM THE BOARD NOW.")
            try: input(">>> Press [ENTER] once powered off... ")
            except EOFError: time.sleep(2.0)

    def _do_energize(self) -> None:
        """Applies physical voltage to the board and captures inrush current.

        If no PSU is connected, pauses FSM execution and prompts the user
        to manually plug in the power cable.
        """
        logger.info(f"[State Machine] Applying RAW POWER to the board (Medium: {self.context.active_boot_medium.upper()})...")

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
            logger.warning("[MANUAL ACTION] PLUG IN THE 12V POWER NOW.")
            try: input(">>> Press [ENTER] once power is applied... ")
            except EOFError: time.sleep(2.0)

        # Securely re-bind the serial port to recover the file descriptor.
        # If the USB-Serial adapter is physically on the board, it drops and re-enumerates during a power cycle.
        self.serial.disconnect()
        for i in range(50):
            try:
                self.serial.connect()
                break
            except Exception:
                if i == 49:
                    raise
                time.sleep(0.1)

    def _set_uboot_trap_and_reboot(self) -> None:
        logger.info("[State Machine] Autoboot Disabled: Hot-patching U-Boot env from OS...")
        self.serial.safe_run("fw_setenv mes_prev_bootcmd \"$(fw_printenv -n bootcmd)\"", timeout_s=3.0, check_exit_code=False)
        self.serial.safe_run("fw_setenv bootcmd 'echo MES Framework Trap'", timeout_s=3.0, check_exit_code=False)
        self.serial.safe_run("reboot", timeout_s=2.0, check_exit_code=False)

    def _restore_uboot_trap(self) -> None:
        logger.info("[State Machine] Restoring original U-Boot environment...")
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
        logger.info("[State Machine] Hunting for Bootloader prompt...")
        if self.serial.is_connected:
            self.serial.raw_set_timeout(0)

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
                logger.info("[State Machine] Autoboot window detected, sniping...")
                for _ in range(3):
                    self.serial.raw_write(blast_bytes)
                    time.sleep(0.05)

            elif isinstance(event, PromptDetected) and event.prompt_type in ("bootloader", "trap"):
                # Synchronize with the prompt via echo
                self.serial.raw_set_timeout(2.0)
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
                logger.info("[State Machine] Bootloader intercepted successfully.")
                return

            elif isinstance(event, BootDataReceived):
                logger.debug(f"[UART] RX <- {event.line}")

        raise BootloaderTimeoutError(
            f"Failed to intercept Bootloader within {self.cfg.cold_boot_timeout_s}s timeout."
        )

    def _event_boot_from_bootloader_to_os(self) -> None:
        """Send the boot command from U-Boot and wait for OS shell via event stream."""
        logger.info(f"[State Machine] Commanding OS Boot: '{self.cfg.bootloader_boot_cmd}'")
        self.serial.flush_buffers()
        self.serial.raw_write(f"{self.cfg.bootloader_boot_cmd}\n".encode())
        self._event_wait_for_os_shell(flush=False)

    def _event_wait_for_os_shell(self, flush: bool = True) -> None:
        """Event-driven OS boot monitor.

        Waits for the Linux shell prompt, handling login/password prompts
        and recording boot profiler milestones along the way.

        Args:
            flush: Whether to flush the UART buffer before reading.
        """
        logger.info("[State Machine] Waiting for Linux Userland & Profiling Boot...")
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
                    logger.info(f"[State Machine] Auto-login shell reached in {event.elapsed_s}s.")
                    return

                elif event.prompt_type == "login":
                    self.boot_metrics["t_boot_total_to_login_s"] = event.elapsed_s
                    time.sleep(0.1)
                    self.serial.write_line(self.cfg.os_user or "root")
                    self.serial.parser.clear_buffer()

                elif event.prompt_type == "password":
                    time.sleep(0.1)
                    self.serial.write_line(self.cfg.get_os_password() or "", sensitive=True)
                    self.serial.parser.clear_buffer()

            elif isinstance(event, BootDataReceived):
                logger.debug(f"[UART] RX <- {event.line}")

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
        logger.info("[State Machine] Establishing primary SSH transport...")
        self.ssh.connect()

    def verify_heartbeat(self) -> bool:
        """Ping the OS state via UART heartbeat to verify it's still alive.

        Returns:
            bool: True if the device successfully echoes the heartbeat payload, False otherwise.
        """
        if not self.serial.is_connected:
            return False
        logger.debug("[State Machine] Verifying UART heartbeat...")
        res = self.serial.safe_run("echo MES_HEARTBEAT", timeout_s=2.0, check_exit_code=False)
        return "MES_HEARTBEAT" in res.stdout

    # =========================================================================
    # SOTA CONTEXT VALIDATION
    # =========================================================================

    def _verify_linux_context(self) -> None:
        """Executes dynamically injected Validators, or defaults to SWUpdate checking.

        Parses the current A/B partition configuration from swupdate and populates
        the DeviceContext. If custom validators are registered, it executes them
        sequentially instead.
        """
        if self.context_validators:
            logger.info(f"[State Machine] Executing {len(self.context_validators)} dynamically injected Context Validators...")
            for validator in self.context_validators:
                try:
                    validator(self)
                except Exception as e:
                    logger.error(f"[State Machine] Custom Context Validator failed: {e}")
            return

        logger.info("[State Machine] Validating default A/B Partitions via SWUpdate IPC...")

        #  THE FIX: Verify 'swupdate' actually exists before parsing its output!
        res_sw = self.serial.safe_run("swupdate -g", timeout_s=3.0, check_exit_code=False)

        if res_sw.ok:
            output = res_sw.stdout.strip()
            shell_prompt = getattr(self.cfg, "os_shell_prompt", "~#")
            lines = [l.strip() for l in output.split('\n') if l.strip() and "swupdate" not in l and shell_prompt not in l]
            self.context.active_rootfs = lines[-1] if lines else "UNKNOWN"
        else:
            logger.debug("[State Machine] swupdate not found or failed. Setting RootFS to UNKNOWN.")
            self.context.active_rootfs = "UNKNOWN"

        crypto_part = getattr(self.cfg, 'storage_data_encrypted', '/dev/mapper/data_crypt')
        if crypto_part:
            mount_res = self.serial.safe_run("mount | grep /data", timeout_s=3.0, check_exit_code=False)
            self.context.crypto_data_mounted = crypto_part in mount_res.stdout

    #
# How to inject context:
## import pytest
##
## @pytest.fixture(autouse=True)
## def bind_custom_os_validator(dut_state_machine):
##     """
##     Automatically injects custom EVSE OS validation into the FSM.
##     Executes seamlessly after the shell is reached.
##     """
##     def custom_uuid_context_parser(fsm):
##         logger.info("Executing custom EVSE UUID verification logic...")
##         uuid_dump = fsm.serial.safe_run("fw_printenv active_uuid", timeout_s=3.0)
##
##         if "1234-abcd" in uuid_dump:
##             fsm.context.active_rootfs = "A"
##         else:
##             fsm.context.active_rootfs = "B"
##
##         # You can even inject arbitrary hardware states for later tests!
##         fsm.context.custom_data["carrier_board_rev"] = "v1.2"
##
##     dut_state_machine.register_context_validator(custom_uuid_context_parser)

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

    def _hw_boot_to_os(self, event: EventData) -> None:
        self.boot_metrics.clear()
        self._align_to_physical_state()

        target_medium = event.kwargs.get("medium", self.context.active_boot_medium)
        needs_strap_change = target_medium != self.context.active_boot_medium

        if needs_strap_change:
            logger.info(f"[State Machine] Boot medium change requested ({target_medium}). Forcing hard reboot.")
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

    def _try_resume_existing_os(self) -> bool:
        """Attempt to reuse an existing OS_USERLAND session. Returns True on success."""
        logger.debug("[State Machine] Verifying UART heartbeat for existing OS_USERLAND state...")
        res = self.serial.safe_run("echo MES_HEARTBEAT", timeout_s=2.0, check_exit_code=False)
        if "MES_HEARTBEAT" in res.stdout:
            try:
                self._finalize_os_boot()
                return True
            except TransportConnectionError:
                logger.warning("[State Machine] SSH provision failed on existing OS. Marking DIRTY.")
        else:
            logger.warning("[State Machine] UART heartbeat failed. OS is a Zombie. Marking DIRTY.")
        return False

    def _try_hot_login(self) -> bool:
        """
        Attempt hot-login from ENERGIZED (at login prompt) state.

        Flushes stale UART data, then writes the configured username directly
        to the waiting login prompt. This avoids the "Login incorrect" cycle
        that an empty ``\\n`` would cause.

        Returns True on success, False if the shell prompt is not reached
        within the configured timeout.
        """
        logger.info("[State Machine] Device is ENERGIZED (Actively Booting or at Login). Intercepting Shell...")

        # 1. Clear any stale bytes that accumulated since the login prompt appeared.
        self.serial.flush_buffers()

        # 2. Write the username directly. The board is already sitting at the
        #    "login:" prompt, so this is the correct next input — not a bare \n.
        logger.debug(f"[State Machine] Hot-Login: sending user '{self.cfg.os_user}'")
        self.serial.write_line(self.cfg.os_user or "root")

        try:
            # 3. _event_wait_for_os_shell handles password challenge (if any)
            #    and blocks until the shell prompt is seen.
            #    Do NOT flush, otherwise the prompt generated by our username will be erased.
            self._event_wait_for_os_shell(flush=False)
            self._finalize_os_boot()
            return True
        except TransportTimeoutError:
            logger.warning("[State Machine] Hot-login failed (shell prompt not reached). Marking DIRTY.")
            return False

    def _hw_to_recovery(self, event: EventData) -> None:
        self._align_to_physical_state()
        if self.state == DutState.RECOVERY: return

        logger.info("[State Machine] Routing to Hardware RECOVERY state. Power cycle required.")
        if self.state != DutState.POWER_OFF: self._do_power_off()

        recovery_pin = getattr(self.cfg, "gpio_recovery_pin", "RECOVERY_BTN")
        if self.gpio and recovery_pin:
            self.gpio.set_pin(recovery_pin, True)
            self._do_energize()
            time.sleep(getattr(self.cfg, "recovery_latch_time_s", 1.5))
            self.gpio.set_pin(recovery_pin, False)
        else:
            logger.warning("="*60)
            logger.warning("[MANUAL ACTION] PRESS AND HOLD THE RECOVERY BUTTON / SET JUMPER NOW.")
            try: input(">>> Press [ENTER] while holding the button... ")
            except EOFError: pass

            self._do_energize()
            logger.warning("[MANUAL ACTION] POWER IS ON. YOU CAN NOW RELEASE THE RECOVERY BUTTON.")

        time.sleep(2.0)
