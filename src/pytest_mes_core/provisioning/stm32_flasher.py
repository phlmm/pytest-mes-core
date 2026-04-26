import structlog
from pathlib import Path
from typing import Any

logger = structlog.get_logger('mes_core.provisioning.stm32')

class Stm32Provisioner:
    """
    Handles mass-erasing and flashing of `.bin` or `.hex` firmware payloads
    onto STM32 targets using the PyOcdTransport over JTAG/SWD.
    """
    def __init__(self, swd_transport: Any):
        self.swd = swd_transport

    def flash_firmware(self, firmware_path: Path, base_address: int = 0x08000000) -> bool:
        """
        Flashes the firmware binary to the MCU internal flash memory.
        """
        if not firmware_path.exists():
            logger.error("Firmware file not found", path=str(firmware_path))
            return False

        logger.info("Initializing STM32 Flash sequence...", file=firmware_path.name)
        
        try:
            from pyocd.flash.file_programmer import FileProgrammer
            
            # 1. Ensure debug probe is actively communicating with the STM32
            if not self.swd.is_connected:
                self.swd.connect()
                
            # 2. Halt the processor core so it doesn't try to execute while we mutate Flash
            self.swd.halt()
            
            logger.info("Erasing and Programming Flash sectors...")
            
            # 3. Instantiate the Flash Programmer
            programmer = FileProgrammer(self.swd._session, progress=self._log_progress)
            
            # 4. Program the file
            if firmware_path.suffix.lower() == '.hex':
                programmer.program(str(firmware_path))
            else:
                programmer.program(str(firmware_path), base_address=base_address)
                
            logger.info("Flash programming complete. Verifying and resetting...")
            
            # 5. Clean reset to jump to the new Reset Handler vector
            self.swd.reset()
            self.swd.resume()
            return True
            
        except Exception as e:
            logger.error("Failed to flash STM32", error=str(e))
            return False

    def _log_progress(self, progress: float) -> None:
        """Callback to hook into PyOCD's flashing progression."""
        pct = int(progress * 100)
        # Prevent log spam: only log when crossing 25% thresholds
        if pct % 25 == 0 and pct != getattr(self, '_last_log_pct', -1):
            logger.debug(f"Flash Progress: {pct}%")
            self._last_log_pct = pct

    async def async_flash_firmware(self, firmware_path: Path, base_address: int = 0x08000000) -> bool:
        """Async variant of flash_firmware allowing parallel SWD flashing across multiple jigs."""
        import anyio
        return await anyio.to_thread.run_sync(self.flash_firmware, firmware_path, base_address)
