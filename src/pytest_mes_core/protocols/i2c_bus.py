import structlog
from typing import List, Union
from pytest_mes_core.transports.base import DutTransport

logger = structlog.get_logger('mes_core.protocols.i2c')

def _format_hex(val: Union[int, str]) -> str:
    if isinstance(val, int):
        return hex(val)
    if isinstance(val, str) and not val.startswith("0x"):
        return f"0x{val}"
    return str(val)

class I2cBus:
    """
    Abstractions for I2C bus validation on embedded Linux devices.
    Wraps the standard i2c-tools (i2cdetect, i2cget, i2cset, i2cdump).
    Includes native asynchronous endpoints for parallel jig execution.
    """
    def __init__(self, dut: DutTransport):
        self.dut = dut

    def detect(self, bus: int) -> List[int]:
        """Scans the specified I2C bus and returns a list of discovered device addresses."""
        logger.debug("scanning_i2c_bus", bus=bus)
        res = self.dut.safe_run(f"i2cdetect -y {bus}", timeout_s=5.0, check_exit_code=True)
        return self._parse_i2cdetect(res.stdout)

    async def async_detect(self, bus: int) -> List[int]:
        """Async variant of detect."""
        logger.debug("scanning_i2c_bus_async", bus=bus)
        res = await self.dut.async_safe_run(f"i2cdetect -y {bus}", timeout_s=5.0, check_exit_code=True)
        return self._parse_i2cdetect(res.stdout)

    def _parse_i2cdetect(self, stdout: str) -> List[int]:
        addresses = []
        for line in stdout.splitlines():
            if ":" not in line:
                continue
            parts = line.split(":")[1].strip().split()
            for p in parts:
                if p != "--" and p != "UU":
                    try:
                        addresses.append(int(p, 16))
                    except ValueError:
                        pass
        return addresses

    def get_byte(self, bus: int, chip_addr: Union[int, str], data_addr: Union[int, str]) -> int:
        """Reads a single byte from a specific register on an I2C device."""
        c_addr = _format_hex(chip_addr)
        d_addr = _format_hex(data_addr)
        res = self.dut.safe_run(f"i2cget -y {bus} {c_addr} {d_addr}", timeout_s=2.0, check_exit_code=True)
        return int(res.stdout.strip(), 16)

    async def async_get_byte(self, bus: int, chip_addr: Union[int, str], data_addr: Union[int, str]) -> int:
        """Async variant of get_byte."""
        c_addr = _format_hex(chip_addr)
        d_addr = _format_hex(data_addr)
        res = await self.dut.async_safe_run(f"i2cget -y {bus} {c_addr} {d_addr}", timeout_s=2.0, check_exit_code=True)
        return int(res.stdout.strip(), 16)

    def set_byte(self, bus: int, chip_addr: Union[int, str], data_addr: Union[int, str], value: Union[int, str]) -> None:
        """Writes a single byte to a specific register on an I2C device."""
        c_addr = _format_hex(chip_addr)
        d_addr = _format_hex(data_addr)
        val = _format_hex(value)
        self.dut.safe_run(f"i2cset -y {bus} {c_addr} {d_addr} {val}", timeout_s=2.0, check_exit_code=True)

    async def async_set_byte(self, bus: int, chip_addr: Union[int, str], data_addr: Union[int, str], value: Union[int, str]) -> None:
        """Async variant of set_byte."""
        c_addr = _format_hex(chip_addr)
        d_addr = _format_hex(data_addr)
        val = _format_hex(value)
        await self.dut.async_safe_run(f"i2cset -y {bus} {c_addr} {d_addr} {val}", timeout_s=2.0, check_exit_code=True)

    def dump(self, bus: int, chip_addr: Union[int, str]) -> str:
        """Dumps all registers of an I2C device."""
        c_addr = _format_hex(chip_addr)
        res = self.dut.safe_run(f"i2cdump -y {bus} {c_addr}", timeout_s=3.0, check_exit_code=True)
        return res.stdout

    async def async_dump(self, bus: int, chip_addr: Union[int, str]) -> str:
        """Async variant of dump."""
        c_addr = _format_hex(chip_addr)
        res = await self.dut.async_safe_run(f"i2cdump -y {bus} {c_addr}", timeout_s=3.0, check_exit_code=True)
        return res.stdout
