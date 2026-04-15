import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional
from transitions import Machine
from enum import Enum, auto

from pytest_mes_core.config import StateMachineConfig, BootProfilerConfig
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply
from pytest_mes_core.transports import EphemeralSerialClient, EphemeralSSHClient
from pytest_mes_core.transports.base import TransportTimeoutError

logger = logging.getLogger("mes_core.state_machine")

class DutState(Enum):
    POWER_OFF = auto()
    ENERGIZED = auto()   # 🚨 NEW: Board has VDD, but framework ignores CPU state
    BOOTLOADER = auto()
    OS_USERLAND = auto()
    RECOVERY = auto()
    DIRTY = auto()

class BaseDutStateMachine(ABC):
    STATES = [DutState.POWER_OFF, DutState.ENERGIZED, DutState.BOOTLOADER, DutState.OS_USERLAND, DutState.DIRTY]

    def __init__(
        self,
        psu: Optional[ScpiPowerSupply],
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

        # 🚨 THE NEW TRANSITION MATRIX
        self.machine.add_transition('power_off', '*', DutState.POWER_OFF, before='_hw_power_off')
        self.machine.add_transition('energize', [DutState.POWER_OFF, DutState.DIRTY], DutState.ENERGIZED, before='_hw_energize')
        self.machine.add_transition('boot_to_bootloader', [DutState.POWER_OFF, DutState.ENERGIZED, DutState.OS_USERLAND, DutState.DIRTY], DutState.BOOTLOADER, before='_hw_boot_to_bootloader')
        self.machine.add_transition('boot_to_os', [DutState.POWER_OFF, DutState.ENERGIZED, DutState.BOOTLOADER, DutState.DIRTY], DutState.OS_USERLAND, before='_hw_boot_to_os')
        self.machine.add_transition('mark_dirty', '*', DutState.DIRTY, before=lambda e: logger.warning(f"[State Machine] Marked DIRTY: {e.kwargs.get('reason', 'Unknown')}"))

    @abstractmethod
    def _hw_power_off(self, event) -> None: pass

    @abstractmethod
    def _hw_energize(self, event) -> None: pass

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
            logger.warning("="*60)
            logger.warning("[State Machine] ⚠️ NO AUTOMATED SCPI PSU CONFIGURED!")
            logger.warning("[State Machine] ⚠️ MANUAL ACTION: UNPLUG THE 12V POWER FROM THE BOARD NOW.")
            logger.warning("="*60)
            try:
                input(">>> Press [ENTER] once the board is completely powered off... ")
            except EOFError:
                logger.warning("[State Machine] Pytest STDIN is captured. Assuming power was dropped.")
                time.sleep(2.0)

    def _hw_energize(self, event) -> None:
        """
        🚨 NEW: Applies raw power so components (like the PIC) have VDD.
        The Linux OS will autoboot in the background, but the framework won't wait for it.
        """
        if self.state not in [DutState.POWER_OFF, DutState.DIRTY]:
            logger.debug("[State Machine] Board is already energized.")
            return

        logger.info("[State Machine] Applying RAW POWER to the board (Ignoring CPU/OS state)...")
        if self.psu:
            self.psu.enable_output()
            logger.debug("[State Machine] Waiting 1.0s for hardware rails to stabilize...")
            time.sleep(1.0)
        else:
            logger.warning("="*60)
            logger.warning("[State Machine] ⚠️ NO AUTOMATED SCPI PSU CONFIGURED!")
            logger.warning("[State Machine] ⚠️ MANUAL ACTION: PLUG IN THE 12V POWER NOW.")
            logger.warning("="*60)
            try:
                input(">>> Press [ENTER] once power is applied... ")
            except EOFError:
                time.sleep(2.0)

    def _hw_boot_to_bootloader(self, event) -> None:
        if self.state == DutState.OS_USERLAND:
            logger.debug("[State Machine] Soft rebooting Linux...")
            if not self.serial.is_connected:
                self.serial.connect()
            self.serial.safe_run("reboot", timeout_s=2.0, check_exit_code=False)
        else:
            # 🚨 FIX: If the board is already energized but we missed the boot window, we MUST power cycle.
            if self.state == DutState.ENERGIZED:
                logger.info("[State Machine] Board is energized. Power cycling to catch BootROM...")
                self._hw_power_off(event)

            if not self.serial.is_connected:
                logger.debug("[State Machine] Binding UART socket to catch BootROM...")
                self.serial.connect()

            if self.psu:
                logger.debug("[SCPI] TX -> ENERGIZING OUTPUT (COLD BOOT)")
                self.psu.enable_output()
            else:
                logger.warning("="*60)
                logger.warning("[State Machine] ⚠️ NO AUTOMATED SCPI PSU CONFIGURED!")
                logger.warning("[State Machine] ⚠️ MANUAL ACTION REQUIRED: RACE CONDITION PREVENTION.")
                logger.warning("="*60)
                try:
                    input(">>> 1. Press [ENTER] on your keyboard NOW to arm the framework...\n>>> 2. THEN immediately plug in the 12V power! ")
                except EOFError:
                    logger.warning("[State Machine] Pytest STDIN is captured.")

        logger.info("[State Machine] Hunting for Bootloader autoboot interrupt (Aggressive Mode)...")

        self.serial.flush_buffers()
        if self.serial.ser and self.serial.ser.is_open:
            self.serial.ser.timeout = 0

        t_end = time.perf_counter() + self.cfg.cold_boot_timeout_s
        bootloader_acquired = False
        blast_bytes = self.cfg.bootloader_interrupt_char.encode('utf-8')

        while time.perf_counter() < t_end:
            try:
                self.serial.ser.write(blast_bytes)
                self.serial.ser.flush()
            except Exception:
                pass

            if self.serial.ser.in_waiting > 0:
                raw_bytes = self.serial.ser.read(self.serial.ser.in_waiting)
                self.serial.parser.ingest(raw_bytes)

                if self.cfg.bootloader_prompt in self.serial.live_buffer:
                    bootloader_acquired = True
                    break

            time.sleep(0.05)

        if not bootloader_acquired:
            logger.critical(f"[State Machine] FATAL: Buffer yielded: {self.serial.live_buffer[-200:]}")
            raise RuntimeError(f"Failed to intercept Bootloader within {self.cfg.cold_boot_timeout_s}s!")

        logger.info("[State Machine] Bootloader prompt detected! Synchronizing...")
        self.serial.ser.timeout = 2.0

        self.serial.ser.write(b"\n")
        self.serial.ser.flush()
        time.sleep(0.1)
        self.serial.flush_buffers()

        res = self.serial.safe_run("echo MES_SYNC", expected_prompt=self.cfg.bootloader_prompt, timeout_s=3.0)
        if "MES_SYNC" not in res:
            raise RuntimeError("Failed to synchronize with Bootloader prompt after interception.")

        logger.info("[State Machine] Bootloader intercepted successfully.")

    def _hw_boot_to_os(self, event) -> None:
        # 🚨 Route through Bootloader if we are currently Energized or Off
        if self.state in [DutState.POWER_OFF, DutState.DIRTY, DutState.ENERGIZED]:
            logger.debug("[State Machine] Routing through Bootloader phase first...")
            self._hw_boot_to_bootloader(event)

        logger.info(f"[State Machine] Commanding OS Boot: '{self.cfg.bootloader_boot_cmd}'")
        logger.debug(f"[UART] TX -> '{self.cfg.bootloader_boot_cmd}'")

        self.serial.flush_buffers()
        self.serial.ser.write(f"{self.cfg.bootloader_boot_cmd}\n".encode())

        logger.info("[State Machine] Waiting for Linux Userland & Profiling Boot...")
        start_time = time.time()
        self.boot_metrics.clear()
        pending_milestones = self.boot_profiler_cfg.milestones.copy() if self.boot_profiler_cfg else {}

        while time.time() - start_time < self.cfg.cold_boot_timeout_s:
            if self.serial.ser.in_waiting > 0:
                raw_bytes = self.serial.ser.read(self.serial.ser.in_waiting)
                self.serial.parser.ingest(raw_bytes)
            else:
                time.sleep(0.01)
                continue

            current_elapsed = round(time.time() - start_time, 3)

            if self.cfg.os_shell_prompt in self.serial.live_buffer:
                self.boot_metrics["t_boot_total_to_shell_s"] = current_elapsed
                logger.info(f"[State Machine] Auto-login shell reached in {current_elapsed}s.")
                break

            elif self.cfg.os_login_prompt in self.serial.live_buffer:
                self.boot_metrics["t_boot_total_to_login_s"] = current_elapsed
                logger.info(f"[State Machine] Login prompt reached in {current_elapsed}s.")
                logger.debug(f"[UART] TX -> '{self.cfg.os_user}' (Providing Username)")
                self.serial.ser.write(f"{self.cfg.os_user}\n".encode())
                self.serial.parser.clear_buffer()

            elif self.cfg.os_password_prompt in self.serial.live_buffer and self.cfg.os_password:
                logger.debug(f"[UART] TX -> '********' (Providing Password)")
                self.serial.ser.write(f"{self.cfg.os_password}\n".encode())
                self.serial.parser.clear_buffer()

            for line in self.serial.parser.extract_lines():
                logger.debug(f"[UART] RX <- {line}")
                if pending_milestones:
                    found_keys = []
                    for name, substring in pending_milestones.items():
                        if substring in line:
                            self.boot_metrics[f"t_boot_{name}_s"] = current_elapsed
                            found_keys.append(name)
                    for k in found_keys:
                        pending_milestones.pop(k)
        else:
            raise TransportTimeoutError("Timed out waiting for Linux Shell prompt.")

        logger.debug("[State Machine] Allowing 2.0s for the Linux network stack and SSH daemon to settle...")
        time.sleep(2.0)

        # 🚨 THE FIXES FROM EARLIER RETAINED HERE
        public_key = getattr(self.cfg, 'os_ssh_public_key', None)

        if public_key:
            logger.info("[State Machine] Injecting Framework SSH Public Key via UART...")
            self.serial.flush_buffers()

            home_dir = getattr(self.cfg, 'os_user_home_dir', '/home/root')
            commands = []

            if getattr(self.cfg, 'immutable_rootfs', False):
                logger.info(f"[State Machine] Immutable RootFS detected. Masking {home_dir} with tmpfs...")
                commands.append(f"mkdir -p {home_dir}")
                commands.append(f"mount -t tmpfs -o mode=755,uid=0,gid=0 tmpfs {home_dir}")

            commands.append(f"mkdir -p {home_dir}/.ssh")
            commands.append(f"chmod 700 {home_dir}/.ssh")
            commands.append(f"echo '{public_key}' > {home_dir}/.ssh/authorized_keys")
            commands.append(f"chmod 600 {home_dir}/.ssh/authorized_keys")
            commands.append(f"chown -R root:root {home_dir}/.ssh")
            commands.append(f"ls -la {home_dir}/.ssh")

            for cmd in commands:
                logger.debug(f"[UART] TX -> {cmd}")
                self.serial.ser.write(f"{cmd}\n".encode('utf-8'))
                time.sleep(0.2)

                if self.serial.ser.in_waiting > 0:
                    resp = self.serial.ser.read(self.serial.ser.in_waiting).decode('utf-8', errors='ignore')
                    for line in resp.split('\n'):
                        if line.strip() and cmd not in line:
                            logger.debug(f"[DUT] {line.strip()}")

            self.serial.flush_buffers()
        else:
            logger.info("[State Machine] Hot-patching root password for SSH fallback...")
            safe_pwd = getattr(self.cfg, 'os_password', 'root')
            self.serial.ser.write(f"echo 'root:{safe_pwd}' | chpasswd\n".encode('utf-8'))
            time.sleep(0.5)

        logger.debug("[State Machine] Binding SSH Transport Matrix...")

        try:
            self.ssh.connect()
        except Exception as e:
            logger.critical("=" * 60)
            logger.critical("[State Machine] SSH REJECTED! Dumping Dropbear security logs from DUT:")
            logger.critical("=" * 60)
            self.serial.flush_buffers()

            # 🚨 Socket-Activated Dropbear fix retained
            self.serial.ser.write(b"journalctl -t dropbear --no-pager -n 20\n")
            time.sleep(1.0)
            if self.serial.ser.in_waiting > 0:
                log_dump = self.serial.ser.read(self.serial.ser.in_waiting).decode('utf-8', errors='ignore')
                for line in log_dump.split('\n'):
                    if line.strip() and "journalctl" not in line:
                        logger.error(f"[DUT-DROPBEAR] {line.strip()}")

            self.serial.ser.write(b"grep root /etc/passwd\n")
            time.sleep(0.5)
            if self.serial.ser.in_waiting > 0:
                passwd_dump = self.serial.ser.read(self.serial.ser.in_waiting).decode('utf-8', errors='ignore')
                logger.error(f"[DUT-PASSWD] {passwd_dump.strip()}")

            logger.critical("=" * 60)
            raise e
