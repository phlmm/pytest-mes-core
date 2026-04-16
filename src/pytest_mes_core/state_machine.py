import time
import re
import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional, Pattern, Any, Callable, List
from dataclasses import dataclass, field
from transitions import Machine
from enum import Enum, auto

from pytest_mes_core.config import StateMachineConfig, BootProfilerConfig
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply
from pytest_mes_core.transports import EphemeralSerialClient, EphemeralSSHClient
from pytest_mes_core.transports.base import TransportTimeoutError

logger = logging.getLogger("mes_core.state_machine")

@dataclass
class DeviceContext:
    active_rootfs: str = "UNKNOWN"
    crypto_data_mounted: bool = False
    active_boot_medium: str = "default"
    custom_data: Dict[str, Any] = field(default_factory=dict)

class DutState(Enum):
    POWER_OFF = auto()
    ENERGIZED = auto()
    BOOTLOADER = auto()
    OS_USERLAND = auto()
    RECOVERY = auto()
    DIRTY = auto()

class BaseDutStateMachine(ABC):
    STATES = [DutState.POWER_OFF, DutState.ENERGIZED, DutState.BOOTLOADER, DutState.OS_USERLAND, DutState.RECOVERY, DutState.DIRTY]
    PANIC_WATCHDOG: Pattern[bytes] = re.compile(br"(Kernel panic - not syncing|Out of memory: Killed process|synchronous external abort)")
    ANSI_ESCAPE_B: Pattern[bytes] = re.compile(br'\x1b\[[0-9;]*[a-zA-Z]')

    def __init__(
        self,
        psu: Optional[ScpiPowerSupply],
        serial: EphemeralSerialClient,
        ssh: EphemeralSSHClient,
        cfg: StateMachineConfig,
        boot_profiler_cfg: Optional[BootProfilerConfig] = None,
        gpio: Optional[Any] = None
    ):
        self.psu = psu
        self.serial = serial
        self.ssh = ssh
        self.cfg = cfg
        self.boot_profiler_cfg = boot_profiler_cfg
        self.gpio = gpio

        self.boot_metrics: Dict[str, float] = {}
        self.context = DeviceContext()

        # 🚨 GENERIC CONTEXT INJECTION REGISTRY
        self.context_validators: List[Callable[['BaseDutStateMachine'], None]] = []

        logger.debug(f"[State Machine] Initializing FSM. PSU: {self.psu is not None} | GPIO: {self.gpio is not None}")

        self.machine = Machine(
            model=self,
            states=self.STATES,
            initial=DutState.DIRTY,
            send_event=True
        )

        self.machine.add_transition('power_off', '*', DutState.POWER_OFF, before='_hw_power_off')
        self.machine.add_transition('energize', '*', DutState.ENERGIZED, before='_hw_energize')
        self.machine.add_transition('boot_to_bootloader', '*', DutState.BOOTLOADER, before='_hw_boot_to_bootloader')
        self.machine.add_transition('boot_to_os', '*', DutState.OS_USERLAND, before='_hw_boot_to_os')
        self.machine.add_transition('boot_to_recovery', '*', DutState.RECOVERY, before='_hw_to_recovery')
        self.machine.add_transition('mark_dirty', '*', DutState.DIRTY, before=lambda e: logger.warning(f"[State Machine] Marked DIRTY."))

    def register_context_validator(self, validator_func: Callable[['BaseDutStateMachine'], None]) -> None:
        """Allows test fixtures to seamlessly inject custom OS validation methods."""
        if validator_func not in self.context_validators:
            self.context_validators.append(validator_func)

    @abstractmethod
    def _hw_power_off(self, event) -> None: pass
    @abstractmethod
    def _hw_energize(self, event) -> None: pass
    @abstractmethod
    def _hw_boot_to_bootloader(self, event) -> None: pass
    @abstractmethod
    def _hw_boot_to_os(self, event) -> None: pass
    @abstractmethod
    def _hw_to_recovery(self, event) -> None: pass


class EmbeddedLinuxStateMachine(BaseDutStateMachine):

    # =========================================================================
    # PHYSICAL PRIMITIVES
    # =========================================================================
    def _sync_physical_state(self) -> None:
        """
        Probes UART to detect actual physical state.
        Crucial for desk setups without PSUs because humans ignore unplug prompts!
        """
        if self.state != DutState.DIRTY and self.psu is not None:
            return

        logger.debug("[State Machine] Probing UART to sync physical state...")
        if not self.serial.is_connected:
            self.serial.connect()
            time.sleep(0.1)

        self.serial.flush_buffers()
        self.serial.ser.write(b"\r\n")
        self.serial.ser.flush()
        time.sleep(0.4)

        resp = bytearray()
        while self.serial.ser.in_waiting > 0:
            resp.extend(self.serial.ser.read(self.serial.ser.in_waiting))
            time.sleep(0.05)

        clean_resp = self.ANSI_ESCAPE_B.sub(b'', resp)

        shell_prompt = getattr(self.cfg, "os_shell_prompt", "~#").encode('utf-8')
        login_prompt = getattr(self.cfg, "os_login_prompt", "login:").encode('utf-8')
        uboot_prompt = getattr(self.cfg, "bootloader_prompt", "=>").encode('utf-8')

        if shell_prompt in clean_resp:
            logger.info("[State Machine] UART Probe: Detected OS Shell. Aligning to OS_USERLAND.")
            self.state = DutState.OS_USERLAND
        elif login_prompt in clean_resp:
            logger.info("[State Machine] UART Probe: Detected Login prompt. Aligning to ENERGIZED.")
            self.state = DutState.ENERGIZED
        elif uboot_prompt in clean_resp:
            logger.info("[State Machine] UART Probe: Detected Bootloader. Aligning to BOOTLOADER.")
            self.state = DutState.BOOTLOADER
        elif len(clean_resp) > 0:
            logger.info("[State Machine] UART Probe: Unknown output. Assumed ENERGIZED.")
            self.state = DutState.ENERGIZED
        else:
            if self.state == DutState.DIRTY:
                logger.info("[State Machine] UART Probe: Silence. Assumed POWER_OFF.")
                self.state = DutState.POWER_OFF

    def _resolve_dirty_state(self) -> None:
        """Non-destructively probes UART to avoid blindly prompting for manual power-cycles."""
        if self.state != DutState.DIRTY:
            return

        logger.debug("[State Machine] State is DIRTY. Probing UART to detect actual physical state...")
        if not self.serial.is_connected:
            self.serial.connect()

        self.serial.flush_buffers()
        self.serial.ser.write(b"\r\n")
        self.serial.ser.flush()
        time.sleep(0.4)

        resp = bytearray()
        while self.serial.ser.in_waiting > 0:
            resp.extend(self.serial.ser.read(self.serial.ser.in_waiting))
            time.sleep(0.05)

        shell_prompt = getattr(self.cfg, "os_shell_prompt", "~#").encode('utf-8')
        login_prompt = getattr(self.cfg, "os_login_prompt", "login:").encode('utf-8')
        uboot_prompt = getattr(self.cfg, "bootloader_prompt", "=>").encode('utf-8')

        if shell_prompt in resp:
            logger.info("[State Machine] UART Probe: Detected OS Shell. Fast-tracking to OS_USERLAND.")
            self.state = DutState.OS_USERLAND
        elif login_prompt in resp:
            logger.info("[State Machine] UART Probe: Detected Login prompt. Resolving to ENERGIZED.")
            self.state = DutState.ENERGIZED
        elif uboot_prompt in resp:
            logger.info("[State Machine] UART Probe: Detected Bootloader. Resolving to BOOTLOADER.")
            self.state = DutState.BOOTLOADER
        elif len(resp) > 0:
            logger.info("[State Machine] UART Probe: Unknown output. Resolving to ENERGIZED.")
            self.state = DutState.ENERGIZED
        else:
            logger.info("[State Machine] UART Probe: Silence. Resolving to POWER_OFF.")
            self.state = DutState.POWER_OFF

    # =========================================================================
    # PHYSICAL PRIMITIVES
    # =========================================================================

    def _do_apply_bootstrap(self, medium: str) -> None:
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

    def _do_power_off(self) -> None:
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
        logger.info(f"[State Machine] Applying RAW POWER to the board (Medium: {self.context.active_boot_medium.upper()})...")
        if not self.serial.is_connected: self.serial.connect()

        if self.psu:
            self.psu.enable_output()
            time.sleep(1.0)
        else:
            logger.warning("[MANUAL ACTION] PLUG IN THE 12V POWER NOW.")
            try: input(">>> Press [ENTER] once power is applied... ")
            except EOFError: time.sleep(2.0)

    def _set_uboot_trap_and_reboot(self) -> None:
        logger.info("[State Machine] Autoboot Disabled: Hot-patching U-Boot env from OS...")
        self.serial.safe_run("fw_setenv mes_prev_bootcmd \"$(fw_printenv -n bootcmd)\"", timeout_s=3.0, check_exit_code=False)
        self.serial.safe_run("fw_setenv bootcmd 'echo MES Framework Trap'", timeout_s=3.0, check_exit_code=False)
        self.serial.safe_run("reboot", timeout_s=2.0, check_exit_code=False)

    def _restore_uboot_trap(self) -> None:
        logger.info("[State Machine] Restoring original U-Boot environment...")
        prompt = getattr(self.cfg, "bootloader_prompt", "=>")
        self.serial.safe_run('setenv bootcmd "${mes_prev_bootcmd}"', expected_prompt=prompt, timeout_s=3.0)
        self.serial.safe_run('setenv mes_prev_bootcmd', expected_prompt=prompt, timeout_s=3.0)
        self.serial.safe_run('saveenv', expected_prompt=prompt, timeout_s=5.0)

    def _do_wait_for_bootloader(self, spam_interrupt: bool) -> None:
        logger.info("[State Machine] Hunting for Bootloader prompt...")
        self.serial.flush_buffers()
        if self.serial.ser and self.serial.ser.is_open:
            self.serial.ser.timeout = 0

        t_end = time.perf_counter() + getattr(self.cfg, "cold_boot_timeout_s", 60.0)
        interrupt_fired = False

        blast_bytes = getattr(self.cfg, 'bootloader_interrupt_char', '\r\n').encode('utf-8')
        prompt_b = getattr(self.cfg, "bootloader_prompt", "=>").encode('utf-8')
        autoboot_msg_b = getattr(self.cfg, 'autoboot_msg', 'Hit any key').encode('utf-8')

        raw_buffer = bytearray()

        while time.perf_counter() < t_end:
            if self.serial.ser.in_waiting > 0:
                chunk = self.serial.ser.read(self.serial.ser.in_waiting)
                raw_buffer.extend(chunk)
                self.serial.parser.ingest(chunk)

                clean_buffer = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)

                if self.PANIC_WATCHDOG.search(clean_buffer):
                    raise RuntimeError(f"Panic during Bootloader routing:\n{clean_buffer[-500:].decode('utf-8', errors='ignore')}")

                if spam_interrupt and not interrupt_fired and autoboot_msg_b in clean_buffer:
                    logger.info("[State Machine] Autoboot window detected, sniping...")
                    for _ in range(3):
                        self.serial.ser.write(blast_bytes)
                        time.sleep(0.05)
                    self.serial.ser.flush()
                    interrupt_fired = True
                    raw_buffer.clear()

                if prompt_b in clean_buffer or b"MES Framework Trap" in clean_buffer:
                    self.serial.ser.timeout = 2.0
                    self.serial.ser.write(b"\n")
                    time.sleep(0.1)
                    self.serial.flush_buffers()

                    res = self.serial.safe_run("echo MES_SYNC", expected_prompt=getattr(self.cfg, "bootloader_prompt", "=>"), timeout_s=3.0)
                    if "MES_SYNC" not in res.stdout:
                        raise RuntimeError("Failed to synchronize with Bootloader prompt.")
                    logger.info("[State Machine] Bootloader intercepted successfully.")
                    return

            time.sleep(0.01)

        raise RuntimeError(f"Failed to intercept Bootloader within timeout.")

    def _do_boot_from_bootloader_to_os(self) -> None:
        logger.info(f"[State Machine] Commanding OS Boot: '{self.cfg.bootloader_boot_cmd}'")
        self.serial.flush_buffers()
        self.serial.ser.write(f"{self.cfg.bootloader_boot_cmd}\n".encode())
        self.serial.ser.flush()
        self._do_wait_for_os()

    def _do_wait_for_os(self) -> None:
        logger.info("[State Machine] Waiting for Linux Userland & Profiling Boot...")
        start_time = time.time()
        self.boot_metrics.clear()
        pending_milestones = self.boot_profiler_cfg.milestones.copy() if self.boot_profiler_cfg else {}

        raw_buffer = bytearray()
        shell_prompt_b = getattr(self.cfg, "os_shell_prompt", "~#").encode('utf-8')
        login_prompt_b = getattr(self.cfg, "os_login_prompt", "login:").encode('utf-8')
        password_prompt_b = getattr(self.cfg, "os_password_prompt", "Password:").encode('utf-8') if getattr(self.cfg, "os_password", None) else None

        login_handled = False
        password_handled = False

        while time.time() - start_time < getattr(self.cfg, "cold_boot_timeout_s", 65.0):
            if self.serial.ser.in_waiting > 0:
                chunk = self.serial.ser.read(self.serial.ser.in_waiting)
                raw_buffer.extend(chunk)
                self.serial.parser.ingest(chunk)

                # 🚨 SOTA FIX: Clean ANSI colors before regex matching!
                clean_buffer = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)

                for line in self.serial.parser.extract_lines():
                    logger.debug(f"[UART] RX <- {line.strip()}")
                    if pending_milestones:
                        found_keys = [k for k, v in pending_milestones.items() if v in line]
                        for k in found_keys:
                            self.boot_metrics[f"t_boot_{k}_s"] = round(time.time() - start_time, 3)
                            pending_milestones.pop(k)

                if self.PANIC_WATCHDOG.search(clean_buffer):
                    raise RuntimeError("Device kernel panicked during OS boot sequence.")
            else:
                time.sleep(0.01)
                continue

            current_elapsed = round(time.time() - start_time, 3)

            # Success Match
            if shell_prompt_b in clean_buffer:
                self.boot_metrics["t_boot_total_to_shell_s"] = current_elapsed
                logger.info(f"[State Machine] Auto-login shell reached in {current_elapsed}s.")
                break

            elif login_prompt_b in clean_buffer and not login_handled:
                self.boot_metrics["t_boot_total_to_login_s"] = current_elapsed
                time.sleep(0.1)
                logger.debug(f"[UART] TX -> '{self.cfg.os_user}'")
                self.serial.ser.write(f"{self.cfg.os_user}\n".encode())
                self.serial.ser.flush()
                login_handled = True
                # We don't wipe raw_buffer here to avoid dropping fast shell prompts
                self.serial.parser.clear_buffer()

            elif password_prompt_b and password_prompt_b in clean_buffer and getattr(self.cfg, "os_password", None) and not password_handled:
                time.sleep(0.1)
                logger.debug("[UART] TX -> '********'")
                self.serial.ser.write(f"{self.cfg.os_password}\n".encode())
                self.serial.ser.flush()
                password_handled = True
                self.serial.parser.clear_buffer()
        else:
            dump = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)[-500:].decode('utf-8', errors='ignore').strip()
            logger.error(f"[State Machine] FATAL TIMEOUT. Clean Buffer dump:\n{dump}")
            raise TransportTimeoutError("Timed out waiting for Linux Shell prompt.")

    def _provision_and_connect_ssh(self) -> None:
        if self.ssh.is_connected: return

        time.sleep(2.0)

        public_key = getattr(self.cfg, 'os_ssh_public_key', None)
        if public_key:
            logger.info("[State Machine] Injecting Framework SSH Public Key via UART...")
            self.serial.flush_buffers()
            home_dir = getattr(self.cfg, 'os_user_home_dir', '/home/root')
            commands = []

            if getattr(self.cfg, 'immutable_rootfs', False):
                commands.extend([f"mkdir -p {home_dir}", f"mount -t tmpfs -o mode=755,uid=0,gid=0 tmpfs {home_dir}"])

            commands.extend([
                f"mkdir -p {home_dir}/.ssh",
                f"chmod 700 {home_dir}/.ssh",
                f"echo '{public_key}' > {home_dir}/.ssh/authorized_keys",
                f"chmod 600 {home_dir}/.ssh/authorized_keys",
                f"chown -R root:root {home_dir}/.ssh"
            ])
            for cmd in commands:
                self.serial.ser.write(f"{cmd}\n".encode('utf-8'))
                time.sleep(0.2)
            self.serial.flush_buffers()
        else:
            safe_pwd = getattr(self.cfg, 'os_password', 'root')
            self.serial.ser.write(f"echo 'root:{safe_pwd}' | chpasswd\n".encode('utf-8'))
            time.sleep(0.5)

        self._verify_linux_context()
        self.ssh.connect()


    # =========================================================================
    # SOTA CONTEXT VALIDATION
    # =========================================================================

    def _verify_linux_context(self) -> None:
        """
        🚨 SOTA: Generic Injection handler.
        Executes dynamically injected Validators, or defaults to SWUpdate checking.
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
        try:
            output = self.serial.safe_run("swupdate -g", timeout_s=3.0, check_exit_code=False).stdout.strip()
            shell_prompt = getattr(self.cfg, "os_shell_prompt", "~#")
            lines = [l.strip() for l in output.split('\n') if l.strip() and "swupdate" not in l and shell_prompt not in l]
            self.context.active_rootfs = lines[-1] if lines else "UNKNOWN"
        except Exception:
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

    def _hw_power_off(self, event) -> None:
        self._sync_physical_state()
        if self.state == DutState.POWER_OFF: return
        self._do_power_off()

    def _hw_energize(self, event) -> None:
        self._sync_physical_state()
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

    def _hw_boot_to_bootloader(self, event) -> None:
        self._sync_physical_state()
        target_medium = event.kwargs.get("medium", self.context.active_boot_medium)
        needs_strap_change = target_medium != self.context.active_boot_medium

        if needs_strap_change or self.state == DutState.RECOVERY:
            self._do_power_off()
            self._do_apply_bootstrap(target_medium)
            self.context.active_boot_medium = target_medium

            if not getattr(self.cfg, 'autoboot_enabled', True):
                self._do_energize()
                self._do_wait_for_os()
                self._provision_and_connect_ssh()
                self._set_uboot_trap_and_reboot()
                self._do_wait_for_bootloader(spam_interrupt=False)
            else:
                self._do_energize()
                self._do_wait_for_bootloader(spam_interrupt=True)
            return

        if self.state == DutState.BOOTLOADER: return

        if not getattr(self.cfg, 'autoboot_enabled', True):
            if self.state != DutState.OS_USERLAND:
                if self.state in [DutState.ENERGIZED]: self._do_power_off()
                self._do_energize()
                self._do_wait_for_os()
            self._provision_and_connect_ssh()
            self._set_uboot_trap_and_reboot()
            self._do_wait_for_bootloader(spam_interrupt=False)
        else:
            if self.state == DutState.OS_USERLAND:
                self.serial.safe_run("reboot", timeout_s=2.0, check_exit_code=False)
            else:
                # 🚨 Desk Fallback: Soft-Reboot from a login prompt without PSU
                if self.state in [DutState.ENERGIZED] and not self.psu:
                    logger.info("[State Machine] Desk Mode: Attempting soft-login to reboot instead of manual power cycle...")
                    self.serial.ser.write(f"{getattr(self.cfg, 'os_user', 'root')}\n".encode())
                    time.sleep(0.5)
                    if getattr(self.cfg, "os_password", None):
                        self.serial.ser.write(f"{self.cfg.os_password}\n".encode())
                        time.sleep(0.5)
                    self.serial.ser.write(b"reboot\n")
                else:
                    if self.state in [DutState.ENERGIZED]: self._do_power_off()
                    self._do_energize()
            self._do_wait_for_bootloader(spam_interrupt=True)

    def _hw_boot_to_os(self, event) -> None:
        self._sync_physical_state()
        target_medium = event.kwargs.get("medium", self.context.active_boot_medium)
        needs_strap_change = target_medium != self.context.active_boot_medium

        if needs_strap_change or self.state == DutState.RECOVERY:
            self._do_power_off()
            self._do_apply_bootstrap(target_medium)
            self.context.active_boot_medium = target_medium
            self._do_energize()

            if getattr(self.cfg, 'autoboot_enabled', True):
                self._do_wait_for_bootloader(spam_interrupt=True)
                self._do_boot_from_bootloader_to_os()
            else:
                self._do_wait_for_os()
            self._provision_and_connect_ssh()
            return

        if self.state == DutState.OS_USERLAND:
            self._provision_and_connect_ssh()
            return

        if self.state == DutState.BOOTLOADER:
            if not getattr(self.cfg, 'autoboot_enabled', True): self._restore_uboot_trap()
            self._do_boot_from_bootloader_to_os()
            self._provision_and_connect_ssh()
            return

        if getattr(self.cfg, 'autoboot_enabled', True):
            # 🚨 Desk Fallback: Soft-Reboot
            if self.state in [DutState.ENERGIZED] and not self.psu:
                logger.info("[State Machine] Desk Mode: Attempting soft-login to reboot instead of manual power cycle...")
                self.serial.ser.write(f"{getattr(self.cfg, 'os_user', 'root')}\n".encode())
                time.sleep(0.5)
                if getattr(self.cfg, "os_password", None):
                    self.serial.ser.write(f"{self.cfg.os_password}\n".encode())
                    time.sleep(0.5)
                self.serial.ser.write(b"reboot\n")
                self._do_wait_for_bootloader(spam_interrupt=True)
                self._do_boot_from_bootloader_to_os()
            else:
                if self.state in [DutState.ENERGIZED]: self._do_power_off()
                self._do_energize()
                self._do_wait_for_bootloader(spam_interrupt=True)
                self._do_boot_from_bootloader_to_os()
        else:
            if self.state in [DutState.ENERGIZED]: self._do_power_off()
            self._do_energize()
            self._do_wait_for_os()

        self._provision_and_connect_ssh()

    def _hw_to_recovery(self, event) -> None:
        self._sync_physical_state()
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
