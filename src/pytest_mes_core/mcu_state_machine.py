import enum
import structlog
from typing import Optional, Protocol, Any
from transitions import Machine

logger = structlog.get_logger('mes_core.mcu_fsm')

class McuState(enum.Enum):
    """
    Physical lifecycle states of a Bare-Metal/RTOS Microcontroller.
    """
    POWER_OFF = "POWER_OFF"       # VCC = 0V
    ENERGIZED = "ENERGIZED"       # VCC > 0V, but core might be in hard-reset
    HALTED = "HALTED"             # Core clock paused via SWD/JTAG probe
    RUNNING = "RUNNING"           # Application firmware executing natively
    OTA_UPDATE = "OTA_UPDATE"     # Core executing MCU Bootloader, waiting for payload
    FAILED_OTA = "FAILED_OTA"     # Core detected invalid CRC/signature, stuck in fallback

class BareMetalStateMachine:
    """
    Finite State Machine orchestrator for Bare-Metal Microcontrollers (STM32, NXP, ESP32).
    
    Provides deterministic hardware transitions between power states, debug states (HALTED),
    and execution states (RUNNING, OTA_UPDATE).
    """

    def __init__(self, psu: Any = None, swd_transport: Any = None):
        self.psu = psu
        self.swd = swd_transport
        
        self.machine = Machine(
            model=self,
            states=[state.value for state in McuState],
            initial=McuState.POWER_OFF.value,
            send_event=True
        )

        # Valid transition matrix
        self.machine.add_transition(trigger='energize', source=McuState.POWER_OFF.value, dest=McuState.ENERGIZED.value, after='_hw_energize')
        self.machine.add_transition(trigger='power_off', source='*', dest=McuState.POWER_OFF.value, after='_hw_power_off')
        
        self.machine.add_transition(trigger='halt_core', source=[McuState.ENERGIZED.value, McuState.RUNNING.value], dest=McuState.HALTED.value, after='_hw_halt_core')
        self.machine.add_transition(trigger='resume_core', source=McuState.HALTED.value, dest=McuState.RUNNING.value, after='_hw_resume_core')
        
        self.machine.add_transition(trigger='trigger_ota', source=[McuState.RUNNING.value, McuState.ENERGIZED.value], dest=McuState.OTA_UPDATE.value, after='_hw_trigger_ota')
        self.machine.add_transition(trigger='ota_corrupt', source=McuState.OTA_UPDATE.value, dest=McuState.FAILED_OTA.value)
        self.machine.add_transition(trigger='ota_success', source=McuState.OTA_UPDATE.value, dest=McuState.RUNNING.value)

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
        """Sets an OTA flag in RAM/RTC register and resets into the Bootloader."""
        logger.info("[MCU] Rebooting into OTA Bootloader mode...")
        if self.swd:
            # Example: Write magic word to SRAM or RTC Backup Register
            # self.swd.write_memory(0x20000000, 0xDEADBEEF)
            self.swd.reset()

    # --- Async Variants for Parallel Jigs ---

    async def async_hw_halt_core(self) -> None:
        """Async variant of halt_core."""
        import anyio
        await anyio.to_thread.run_sync(self.halt_core)

    async def async_hw_resume_core(self) -> None:
        """Async variant of resume_core."""
        import anyio
        await anyio.to_thread.run_sync(self.resume_core)
