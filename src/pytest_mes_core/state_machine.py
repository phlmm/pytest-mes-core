# src/pytest_mes_core/state_machine.py
import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional
from transitions import Machine

from pytest_mes_core.config import StateMachineConfig, BootProfilerConfig
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply
from pytest_mes_core.transports import EphemeralSerialClient, EphemeralSSHClient
from pytest_mes_core.transports.base import TransportTimeoutError
from enum import Enum, auto

logger = logging.getLogger("mes_core.state_machine")

class DutState(Enum):
    POWER_OFF = auto()
    BOOTLOADER = auto()
    OS_USERLAND = auto()
    RECOVERY = auto()
    DIRTY = auto()

class BaseDutStateMachine(ABC):
    STATES = [DutState.POWER_OFF, DutState.BOOTLOADER, DutState.OS_USERLAND, DutState.DIRTY]

    def __init__(
        self,
        psu: Optional[ScpiPowerSupply], # THE FIX: PSU is now strictly Optional
        serial: EphemeralSerialClient,
        ssh: EphemeralSSHClient,
        cfg: StateMachineConfig,
        boot_profiler_cfg: Optional[BootProfilerConfig] = None
    ):
        self.psu = psu
        self.serial = serial
        self.ssh = ssh
        self.cfg = cfg
        self.boot_profiler_cfg = boot_profiler_cfg
        self.boot_metrics: Dict[str, float] = {}

        logger.debug(f"[State Machine] Initializing FSM matrix. Automated PSU present: {self.psu is not None}")

        self.machine = Machine(
            model=self,
            states=self.STATES,
            initial=DutState.DIRTY,
            send_event=True
        )

        self.machine.add_transition('power_off', '*', DutState.POWER_OFF, before='_hw_power_off')
        self.machine.add_transition('boot_to_bootloader', [DutState.POWER_OFF, DutState.OS_USERLAND, DutState.DIRTY], DutState.BOOTLOADER, before='_hw_boot_to_bootloader')
        self.machine.add_transition('boot_to_os', [DutState.POWER_OFF, DutState.BOOTLOADER, DutState.DIRTY], DutState.OS_USERLAND, before='_hw_boot_to_os')
        self.machine.add_transition('mark_dirty', '*', DutState.DIRTY, before=lambda e: logger.warning(f"[State Machine] Marked DIRTY: {e.kwargs.get('reason', 'Unknown')}"))

    @abstractmethod
    def _hw_power_off(self, event) -> None: pass

    @abstractmethod
    def _hw_boot_to_bootloader(self, event) -> None: pass

    @abstractmethod
    def _hw_boot_to_os(self, event) -> None: pass


class EmbeddedLinuxStateMachine(BaseDutStateMachine):
    def _hw_power_off(self, event) -> None:
        logger.debug("[State Machine] Executing hard power drop. Severing network sockets...")
        if self.ssh.is_connected:
            self.ssh.disconnect()

        if self.psu:
            logger.debug("[SCPI] TX -> COMMANDING 0.0V (POWER OFF)")
            self.psu.set_voltage(0.0)
            self.psu.disable_output()
            logger.debug("[State Machine] Waiting 2.0s for bulk decoupling capacitors to discharge...")
            time.sleep(2.0)
        else:
            # 🚨 MANUAL INTERVENTION INTERCEPTOR 🚨
            logger.warning("="*60)
            logger.warning("[State Machine] ⚠️ NO AUTOMATED SCPI PSU CONFIGURED!")
            logger.warning("[State Machine] ⚠️ MANUAL ACTION: UNPLUG THE 12V POWER FROM THE BOARD NOW.")
            logger.warning("="*60)
            try:
                input(">>> Press [ENTER] once the board is completely powered off... ")
            except EOFError:
                logger.warning("[State Machine] Pytest STDIN is captured. Assuming power was dropped.")
                time.sleep(2.0)

    def _hw_boot_to_bootloader(self, event) -> None:
        if self.state == DutState.OS_USERLAND:
            # Notice how a soft-reboot doesn't need a PSU at all!
            logger.debug("[State Machine] Soft rebooting Linux...")
            logger.debug("[UART] TX -> 'reboot'")
            self.serial.safe_run("reboot", timeout_s=2.0, check_exit_code=False)
        else:
            if self.psu:
                logger.debug("[SCPI] TX -> ENERGIZING OUTPUT (COLD BOOT)")
                self.psu.enable_output()
            else:
                # 🚨 MANUAL INTERVENTION INTERCEPTOR 🚨
                logger.warning("="*60)
                logger.warning("[State Machine] ⚠️ NO AUTOMATED SCPI PSU CONFIGURED!")
                logger.warning("[State Machine] ⚠️ MANUAL ACTION: PLUG IN THE 12V POWER TO THE BOARD NOW.")
                logger.warning("="*60)
                try:
                    input(">>> Press [ENTER] immediately after applying power... ")
                except EOFError:
                    logger.warning("[State Machine] Pytest STDIN is captured. Assuming power was applied.")
                    time.sleep(0.5)

        logger.info("[State Machine] Hunting for Bootloader autoboot interrupt...")

        try:
            # 1. Wait for interrupt and blast the stop character
            self.serial.expect(
                pattern=self.cfg.bootloader_interrupt_pattern,
                timeout_s=self.cfg.cold_boot_timeout_s,
                blast_char=self.cfg.bootloader_interrupt_char
            )

            # 2. Wait for the prompt
            self.serial.expect(self.cfg.bootloader_prompt, timeout_s=5.0)

            # 3. Verify control
            res = self.serial.safe_run("echo MES_SYNC", expected_prompt=self.cfg.bootloader_prompt)
            if "MES_SYNC" not in res:
                raise TransportTimeoutError("Failed to synchronize with Bootloader prompt.")

            logger.info("[State Machine] Bootloader intercepted successfully.")

        except Exception as e:
            logger.critical("="*60)
            logger.critical(f"[State Machine] FATAL: Failed to intercept Bootloader!")
            logger.critical("="*60)
            raise RuntimeError(f"Bootloader intercept failed: {e}")

    def _hw_boot_to_os(self, event) -> None:
        if self.state in [DutState.POWER_OFF, DutState.DIRTY]:
            logger.debug("[State Machine] Board is cold. Routing through Bootloader phase first...")
            self._hw_boot_to_bootloader(event)

        logger.info(f"[State Machine] Commanding OS Boot: '{self.cfg.bootloader_boot_cmd}'")
        logger.debug(f"[UART] TX -> '{self.cfg.bootloader_boot_cmd}'")
        self.serial.ser.write(f"{self.cfg.bootloader_boot_cmd}\n".encode())

        logger.info("[State Machine] Waiting for Linux Userland & Profiling Boot...")
        start_time = time.time()
        self.boot_metrics.clear()

        pending_milestones = self.boot_profiler_cfg.milestones.copy() if self.boot_profiler_cfg else {}

        while time.time() - start_time < self.cfg.cold_boot_timeout_s:
            # Readline is perfect here. It captures exactly one line of the dmesg/kernel boot log.
            line = self.serial.ser.readline().decode('utf-8', errors='replace').strip()
            if not line:
                continue

            current_elapsed = round(time.time() - start_time, 3)

            # MATRIX TRACE: Stream the raw kernel boot log to the host console
            logger.debug(f"[UART] RX <- {line}")

            # Inline Boot Profiler
            if pending_milestones:
                found_keys = []
                for name, substring in pending_milestones.items():
                    if substring in line:
                        self.boot_metrics[f"t_boot_{name}_s"] = current_elapsed

                        # High-visibility trace for milestone intercepts
                        logger.debug("-" * 40)
                        logger.debug(f"[Boot Profiler] ⏱️ MILESTONE REACHED: '{name}'")
                        logger.debug(f"[Boot Profiler] ⏱️ Matched Regex: '{substring}'")
                        logger.debug(f"[Boot Profiler] ⏱️ Elapsed Time: {current_elapsed}s")
                        logger.debug("-" * 40)

                        found_keys.append(name)
                for k in found_keys:
                    pending_milestones.pop(k)

            # State Resolution
            if self.cfg.os_shell_prompt in line:
                self.boot_metrics["t_boot_total_to_shell_s"] = current_elapsed
                logger.info(f"[State Machine] Auto-login shell reached in {current_elapsed}s.")
                break

            elif self.cfg.os_login_prompt in line:
                self.boot_metrics["t_boot_total_to_login_s"] = current_elapsed
                logger.info(f"[State Machine] Login prompt reached in {current_elapsed}s.")

                logger.debug(f"[UART] TX -> '{self.cfg.os_user}' (Providing Username)")
                self.serial.ser.write(f"{self.cfg.os_user}\n".encode())

            elif self.cfg.os_password_prompt in line and self.cfg.os_password:
                logger.debug(f"[UART] TX -> '********' (Providing Password)")
                self.serial.ser.write(f"{self.cfg.os_password}\n".encode())
        else:
            raise TransportTimeoutError("Timed out waiting for Linux Shell prompt.")

        logger.debug("[State Machine] Allowing 2.0s for the Linux network stack and SSH daemon to settle...")
        time.sleep(2.0)

        logger.debug("[State Machine] Binding SSH Transport Matrix...")
        self.ssh.connect()
