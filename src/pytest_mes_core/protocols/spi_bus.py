import anyio
import structlog
from typing import Union, List
from pytest_mes_core.transports.base import DutTransport

logger = structlog.get_logger('mes_core.protocols.spi')

class SpiBus:
    """
    Abstractions for SPI bus validation on embedded Linux devices.
    Wraps spi-tools (spi-config, spi-pipe) commonly used for testing.
    Includes native asynchronous endpoints for parallel jig execution.
    """
    def __init__(self, dut: DutTransport):
        self.dut = dut

    def config(self, device: str, mode: int = 0, bits: int = 8, speed_hz: int = 1000000) -> None:
        """Configures the SPI device using spi-config."""
        logger.debug("configuring_spi", device=device, mode=mode, bits=bits, speed_hz=speed_hz)
        cmd = f"spi-config -d {device} -m {mode} -b {bits} -s {speed_hz}"
        self.dut.safe_run(cmd, timeout_s=2.0, check_exit_code=True)


    def transfer(self, device: str, tx_hex_bytes: List[str]) -> List[str]:
        """
        Transfers data over SPI using spi-pipe.
        tx_hex_bytes should be a list of hex strings like ['01', 'FF', 'A2'].
        Returns the received bytes as a list of hex strings.
        """
        if not tx_hex_bytes:
            return []
            
        hex_str = "".join(f"\\x{b}" for b in tx_hex_bytes)
        # We pipe the hex bytes into spi-pipe, and then pipe the output to od to read it.
        # "od -An -v -t x1" prints raw bytes in hex without addresses.
        cmd = f"echo -ne '{hex_str}' | spi-pipe -d {device} | od -An -v -t x1"
        res = self.dut.safe_run(cmd, timeout_s=3.0, check_exit_code=True)
        
        return self._parse_od_output(res.stdout)


    def _parse_od_output(self, stdout: str) -> List[str]:
        rx_bytes = []
        for line in stdout.splitlines():
            parts = line.strip().split()
            for p in parts:
                rx_bytes.append(p.zfill(2).upper())
        return rx_bytes
