import struct
import structlog
import time
import serial
from typing import Optional, Tuple, Dict, Any
from functools import partial
import anyio

logger = structlog.get_logger('mes_core.instruments.fnirsi_dps150')

class FnirsiProtocolError(Exception):
    pass

class FnirsiDPS150:
    """
    Control library for FNIRSI DPS-150 / FNB58 power supplies.
    Uses the official USB/Serial binary protocol.
    """
    
    HEADER_REQ = 0xF1
    HEADER_RES = 0xF0
    
    CMD_READ = 0xA1
    CMD_WRITE_FLOAT = 0xB1
    CMD_WRITE_BYTE = 0xB1
    CMD_CONFIG = 0xC0
    CMD_CONNECT = 0xC1
    
    REG_BAUD_RATE = 0x00
    REG_SET_VOLTAGE = 0xC1
    REG_SET_CURRENT = 0xC2
    REG_LIVE_VALUES = 0xC3
    REG_TEMPERATURE = 0xC4
    REG_OVP_LIMIT = 0xD1
    REG_OCP_LIMIT = 0xD2
    REG_OPP_LIMIT = 0xD3
    REG_OTP_LIMIT = 0xD4
    REG_BRIGHTNESS = 0xD6
    REG_CAPACITY_AH = 0xD9
    REG_CAPACITY_WH = 0xDA
    REG_OUTPUT_STATE = 0xDB
    REG_PROTECTION = 0xDC
    REG_MODE = 0xDD
    REG_SERIAL = 0xDF
    REG_FIRMWARE = 0xE0
    REG_DEVICE_ID = 0xE1
    REG_MAX_VOLTAGE = 0xE2
    REG_MAX_CURRENT = 0xE3
    REG_ALL = 0xFF

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.5):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None
        self._last_voltage: Optional[float] = None
        self._last_current: Optional[float] = None
        
    def connect(self) -> None:
        """Opens serial port and sends the connect command to the device."""
        logger.debug("fnirsi_connecting", port=self.port, baudrate=self.baudrate)
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        except serial.SerialException as e:
            logger.error("fnirsi_serial_open_failed", port=self.port, error=str(e))
            raise InstrumentConnectionError(f"Failed to open port {self.port}: {e}") from e
            
        # Send CONNECT packet: Data=0x01
        self._send_packet(self.CMD_CONNECT, self.REG_BAUD_RATE, b'\x01')
        time.sleep(0.5) # Wait after connect as per protocol documentation
        logger.info("fnirsi_connected", port=self.port)

    def disconnect(self) -> None:
        """Sends disconnect command and closes serial port."""
        if self.ser and self.ser.is_open:
            try:
                self._send_packet(self.CMD_CONNECT, self.REG_BAUD_RATE, b'\x00')
            except Exception as e:
                logger.warning("fnirsi_disconnect_failed", error=str(e))
            finally:
                self.ser.close()
                self.ser = None
                logger.info("fnirsi_disconnected", port=self.port)

    def _calc_checksum(self, register: int, data: bytes) -> int:
        return (register + len(data) + sum(data)) & 0xFF

    def _send_packet(self, cmd_type: int, register: int, data: bytes) -> None:
        if not self.ser or not self.ser.is_open:
            raise FnirsiProtocolError("Serial port is not open")
            
        checksum = self._calc_checksum(register, data)
        packet = bytes([self.HEADER_REQ, cmd_type, register, len(data)]) + data + bytes([checksum])
        
        logger.debug("fnirsi_tx", hex=packet.hex())
        self.ser.write(packet)
        self.ser.flush()
        # protocol advises 50-100ms pause between commands for reliability
        time.sleep(0.05)

    def _read_response(self, expected_cmd: int, expected_reg: int) -> bytes:
        if not self.ser or not self.ser.is_open:
            raise FnirsiProtocolError("Serial port is not open")
            
        t0 = time.time()
        while time.time() - t0 < self.timeout:
            # Sync to 0xF0
            b = self.ser.read(1)
            if not b:
                continue
            if b[0] != self.HEADER_RES:
                continue
                
            hdr_rem = self.ser.read(3)
            if len(hdr_rem) < 3:
                continue
            
            cmd_type = hdr_rem[0]
            register = hdr_rem[1]
            data_len = hdr_rem[2]
            
            payload = self.ser.read(data_len + 1)
            if len(payload) < data_len + 1:
                continue
                
            data = payload[:-1]
            checksum = payload[-1]
            
            calc_cs = self._calc_checksum(register, data)
            if checksum != calc_cs:
                continue # Bad checksum, ignore and re-sync
                
            if expected_cmd is not None and cmd_type != expected_cmd:
                continue
            if expected_reg is not None and register != expected_reg:
                continue
                
            return data
            
        raise FnirsiProtocolError(f"Timeout waiting for response (cmd=0x{expected_cmd:02X}, reg=0x{expected_reg:02X})")

    def _float_to_bytes(self, value: float) -> bytes:
        return struct.pack('<f', value)

    def _bytes_to_float(self, data: bytes) -> float:
        return struct.unpack('<f', data)[0]

    def set_ovp_limit(self, voltage: float) -> None:
        """Sets the Over Voltage Protection (OVP) limit."""
        # The firmware does not support setting OVP/OCP limits as floats over UART.
        # Registers 0xD1/0xD2 only accept 1-byte boolean toggles (0x00=OFF, 0x01=ON).
        logger.warning("fnirsi_ovp_limit_float_unsupported", requested_v=voltage)
        self._send_packet(self.CMD_WRITE_BYTE, self.REG_OVP_LIMIT, b'\x01')

    def set_ocp_limit(self, current: float) -> None:
        """Sets the Over Current Protection (OCP) limit."""
        logger.warning("fnirsi_ocp_limit_float_unsupported", requested_a=current)
        self._send_packet(self.CMD_WRITE_BYTE, self.REG_OCP_LIMIT, b'\x01')

    def set_voltage(self, voltage: float) -> None:
        """Sets the target voltage in Volts.
        
        A voltage of 0.0 is silently ignored — the DPS150 triggers an
        undervoltage protection fault if the setpoint is driven to zero
        while the output is still enabled.  Use disable_output() instead.
        """
        if voltage <= 0:
            logger.debug("fnirsi_set_voltage_zero_ignored", hint="use disable_output()")
            return
        self._last_voltage = voltage
        data = self._float_to_bytes(voltage)
        self._send_packet(self.CMD_WRITE_FLOAT, self.REG_SET_VOLTAGE, data)
        logger.debug("fnirsi_set_voltage", voltage=voltage)

    def set_current(self, current: float) -> None:
        """Sets the target current limit in Amperes.
        
        Only caches positive values (same rationale as set_voltage).
        """
        if current > 0:
            self._last_current = current
        data = self._float_to_bytes(current)
        self._send_packet(self.CMD_WRITE_FLOAT, self.REG_SET_CURRENT, data)
        logger.debug("fnirsi_set_current", current=current, cached=self._last_current)

    def enable_output(self, enable: bool = True) -> None:
        """Enables or disables the power output.
        
        When enabling, restores the last known positive setpoints before
        sending the OUTPUT ON command — the DPS150 firmware clears its
        volatile registers on output-off.
        """
        if enable:
            if self._last_voltage is not None:
                data = self._float_to_bytes(self._last_voltage)
                self._send_packet(self.CMD_WRITE_FLOAT, self.REG_SET_VOLTAGE, data)
                logger.debug("fnirsi_restore_voltage", voltage=self._last_voltage)
            if self._last_current is not None:
                data = self._float_to_bytes(self._last_current)
                self._send_packet(self.CMD_WRITE_FLOAT, self.REG_SET_CURRENT, data)
                logger.debug("fnirsi_restore_current", current=self._last_current)
                
        val = b'\x01' if enable else b'\x00'
        self._send_packet(self.CMD_WRITE_BYTE, self.REG_OUTPUT_STATE, val)
        logger.debug("fnirsi_output_enabled", enabled=enable)
        
    def disable_output(self) -> None:
        self.enable_output(False)

    def read_live_values(self) -> Tuple[float, float, float]:
        """
        Reads real-time measurements.
        Returns: (voltage_V, current_A, power_W)
        """
        self._send_packet(self.CMD_READ, self.REG_LIVE_VALUES, b'\x00')
        data = self._read_response(self.CMD_READ, self.REG_LIVE_VALUES)
        if len(data) < 12:
            raise FnirsiProtocolError("Live values response too short")
            
        v = self._bytes_to_float(data[0:4])
        a = self._bytes_to_float(data[4:8])
        w = self._bytes_to_float(data[8:12])
        return v, a, w

    def measure_voltage(self) -> float:
        """Alias to read live voltage (compatibility)."""
        v, a, w = self.read_live_values()
        return v

    def measure_current(self) -> float:
        """Alias to read live current (compatibility)."""
        v, a, w = self.read_live_values()
        return a

    def set_current_limit(self, amps: float) -> None:
        """Alias for set_current (compatibility)."""
        self.set_current(amps)

    def close(self) -> None:
        """Alias for disconnect (compatibility)."""
        self.disconnect()

    def set_brightness(self, brightness: int) -> None:
        """Sets display brightness (0-20)."""
        b = max(0, min(20, brightness))
        self._send_packet(self.CMD_WRITE_BYTE, self.REG_BRIGHTNESS, bytes([b]))
        
    def read_all_parameters(self) -> Dict[str, Any]:
        """Reads the full register dump and parses parameters."""
        self._send_packet(self.CMD_READ, self.REG_ALL, b'\x00')
        data = self._read_response(self.CMD_READ, self.REG_ALL)
        if len(data) < 110:
            raise FnirsiProtocolError("All parameters response too short")
            
        res = {
            'input_voltage': self._bytes_to_float(data[0:4]),
            'set_voltage': self._bytes_to_float(data[4:8]),
            'set_current': self._bytes_to_float(data[8:12]),
            'output_voltage': self._bytes_to_float(data[12:16]),
            'output_current': self._bytes_to_float(data[16:20]),
            'output_power': self._bytes_to_float(data[20:24]),
            'temperature': self._bytes_to_float(data[24:28]),
            'max_voltage': self._bytes_to_float(data[76:80]),
            'max_current': self._bytes_to_float(data[80:84]),
            'opp_limit': self._bytes_to_float(data[84:88]),
            'ovp_limit': self._bytes_to_float(data[88:92]),
            'ocp_limit': self._bytes_to_float(data[92:96]),
            'output_enabled': data[107] == 1,
            'mode': 'CC' if data[108] == 1 else 'CV',
            'protection_status': data[109]
        }
        return res

    # ------------------------------------------------------------------
    # Async API (anyio-compatible)
    # ------------------------------------------------------------------

    async def async_connect(self) -> None:
        await anyio.to_thread.run_sync(self.connect)

    async def async_set_voltage(self, volts: float) -> None:
        await anyio.to_thread.run_sync(partial(self.set_voltage, volts))

    async def async_set_current_limit(self, amps: float) -> None:
        await anyio.to_thread.run_sync(partial(self.set_current_limit, amps))

    async def async_enable_output(self) -> None:
        await anyio.to_thread.run_sync(self.enable_output)

    async def async_disable_output(self) -> None:
        await anyio.to_thread.run_sync(self.disable_output)

    async def async_measure_current(self) -> float:
        return await anyio.to_thread.run_sync(self.measure_current)

    async def async_measure_voltage(self) -> float:
        return await anyio.to_thread.run_sync(self.measure_voltage)

    async def async_close(self) -> None:
        await anyio.to_thread.run_sync(self.close)

class InstrumentConnectionError(FnirsiProtocolError):
    pass
