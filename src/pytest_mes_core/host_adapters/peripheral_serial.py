import serial # type: ignore
import time
import logging
from typing import Optional
from pytest_mes_core.config import HostSerialConfig

logger = logging.getLogger("mes_core.host_adapters.peripheral_serial")

class HostPeripheralSerialAdapter:
    """Raw binary serial adapter for testing RS485/UART data buses."""
    def __init__(self, cfg: HostSerialConfig):
        self.cfg = cfg
        self.ser: Optional[serial.Serial] = None

    def connect(self) -> None:
        if self.ser: return
        logger.info(f"[Host RS485] Binding on {self.cfg.port} @ {self.cfg.baudrate}bps")
        self.ser = serial.Serial(self.cfg.port, self.cfg.baudrate, timeout=self.cfg.timeout_s, exclusive=False)

    def disconnect(self) -> None:
        if self.ser:
            self.ser.close()
            self.ser = None

    def clear_rx_buffer(self) -> None:
        if self.ser: self.ser.reset_input_buffer()

    def send(self, payload: bytes) -> None:
        if self.ser:
            self.ser.write(payload)
            self.ser.flush()

    def expect(self, payload: bytes, timeout_s: float = 2.0) -> bool:
        if not self.ser: return False
        t_end = time.perf_counter() + timeout_s
        buf = bytearray()
        while time.perf_counter() < t_end:
            if self.ser.in_waiting > 0:
                buf.extend(self.ser.read(self.ser.in_waiting))
                if payload in buf:
                    return True
            time.sleep(0.01)
        return False
