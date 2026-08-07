import enum
import structlog
from typing import Optional, Any
import time
from transitions import Machine

logger = structlog.get_logger('mes_core.mcu_fsm')

class McuState(enum.Enum):
    """
    Physical lifecycle states of a Bare-Metal/RTOS Microcontroller.
    """
    POWER_OFF  = "POWER_OFF"    # VCC = 0V
    ENERGIZED  = "ENERGIZED"    # VCC > 0V, core in reset or auto-running
    HALTED     = "HALTED"       # Core clock paused via SWD/JTAG probe
    RUNNING    = "RUNNING"      # Application firmware executing natively
    OTA_UPDATE = "OTA_UPDATE"   # Core executing MCU Bootloader, waiting for payload
    FAILED_OTA = "FAILED_OTA"   # Core detected invalid CRC/signature, stuck in fallback


class BareMetalStateMachine:
    """
    Finite State Machine orchestrator for Bare-Metal Microcontrollers (STM32, NXP, ESP32).

    Provides deterministic hardware transitions between power states, debug states
    (HALTED), and execution states (RUNNING, OTA_UPDATE).

    Design notes:
    - After energize(), most MCUs auto-run firmware; use ``auto_run`` to reflect this.
    - After a failed OTA, the ``recover_ota`` trigger power-cycles back to ENERGIZED so
      the operator/test can retry without manual intervention.
    - Async variants offload blocking SWD calls to a thread via anyio so the event
      loop is never blocked during parallel jig operations.
    """

    def __init__(
        self,
        psu: Any = None,
        swd_transport: Any = None,
        ota_flag_addr: Optional[int] = None,
        ota_flag_value: Optional[bytes] = None,
    ):
        self.psu = psu
        self.swd = swd_transport
        self.ota_flag_addr = ota_flag_addr
        self.ota_flag_value = ota_flag_value

        self.machine = Machine(
            model=self,
            states=[state.value for state in McuState],
            initial=McuState.POWER_OFF.value,
            send_event=True,
            after_state_change='_record_timestamp',
        )

        # --- Power ---
        self.machine.add_transition(
            trigger='energize',
            source=McuState.POWER_OFF.value,
            dest=McuState.ENERGIZED.value,
            before='_hw_energize',
        )
        self.machine.add_transition(
            trigger='power_off',
            source='*',
            dest=McuState.POWER_OFF.value,
            before='_hw_power_off',
        )

        # --- Auto-run: MCUs run firmware immediately after power-on (no SWD needed) ---
        self.machine.add_transition(
            trigger='auto_run',
            source=McuState.ENERGIZED.value,
            dest=McuState.RUNNING.value,
        )

        # --- Debug probe control ---
        self.machine.add_transition(
            trigger='halt_core',
            source=[McuState.ENERGIZED.value, McuState.RUNNING.value],
            dest=McuState.HALTED.value,
            before='_hw_halt_core',
        )
        self.machine.add_transition(
            trigger='resume_core',
            source=McuState.HALTED.value,
            dest=McuState.RUNNING.value,
            before='_hw_resume_core',
        )

        # --- OTA ---
        self.machine.add_transition(
            trigger='trigger_ota',
            source=[McuState.RUNNING.value, McuState.ENERGIZED.value],
            dest=McuState.OTA_UPDATE.value,
            before='_hw_trigger_ota',
        )
        self.machine.add_transition(
            trigger='ota_corrupt',
            source=McuState.OTA_UPDATE.value,
            dest=McuState.FAILED_OTA.value,
        )
        self.machine.add_transition(
            trigger='ota_success',
            source=McuState.OTA_UPDATE.value,
            dest=McuState.RUNNING.value,
        )

        # --- FAILED_OTA recovery (BUG: previously terminal state with no exit) ---
        self.machine.add_transition(
            trigger='recover_ota',
            source=McuState.FAILED_OTA.value,
            dest=McuState.POWER_OFF.value,
            before='_hw_power_off',
        )

    def _record_timestamp(self, event: Any) -> None:
        from pytest_mes_core.events import bus, StateChanged
        ev = StateChanged(
            fsm_name=self.__class__.__name__,
            old_state=event.transition.source,
            new_state=self.state,
            trigger=event.event.name,
            timestamp=time.time()
        )
        bus.emit_state_event(event=ev)

    # --- Hardware Execution Callbacks ---

    def _hw_energize(self, event: Any) -> None:
        """Applies physical voltage to the MCU."""
        logger.info("[MCU] Energizing VCC...")
        if self.psu:
            self.psu.enable_output()

    def _hw_power_off(self, event: Any) -> None:
        """Severs power to the MCU."""
        logger.info("[MCU] Severing VCC...")
        if self.psu:
            self.psu.disable_output()

    def _hw_halt_core(self, event: Any) -> None:
        """Issues SWD HALT command to pause the program counter."""
        logger.info("[MCU] Halting CPU Core via Debug Probe...")
        if self.swd:
            self.swd.halt()

    def _hw_resume_core(self, event: Any) -> None:
        """Issues SWD RESUME command to continue execution."""
        logger.info("[MCU] Resuming CPU Core execution...")
        if self.swd:
            self.swd.resume()

    def _hw_trigger_ota(self, event: Any) -> None:
        """Sets an OTA flag in RAM/RTC register and resets into the Bootloader.

        If ``ota_flag_addr``/``ota_flag_value`` were supplied at construction
        time, this actually writes that magic value to the given address via
        the SWD transport's ``write_memory()`` before resetting, so the MCU
        Bootloader can detect it on the next reset. If they are not
        configured, no flag is written here — it is the caller's
        responsibility to have set it via some other means (e.g. firmware
        pre-arming its own flag) before triggering this transition.
        """
        logger.info("[MCU] Rebooting into OTA Bootloader mode...")
        if self.swd:
            if (
                self.ota_flag_addr is not None
                and self.ota_flag_value is not None
                and hasattr(self.swd, "write_memory")
            ):
                logger.debug(
                    "[MCU] Writing OTA flag",
                    addr=hex(self.ota_flag_addr),
                    value=self.ota_flag_value,
                )
                self.swd.write_memory(self.ota_flag_addr, self.ota_flag_value)
            self.swd.reset()

