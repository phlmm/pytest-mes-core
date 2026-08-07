import struct
import structlog
from typing import Optional, Callable, Type, TypeVar, ClassVar
from pydantic import BaseModel
from .base import RpcClientBase

logger = structlog.get_logger('mes_core.protocols.rpc.mcu')

TMessage = TypeVar('TMessage', bound='McuRpcMessage')

class McuRpcMessage(BaseModel):
    """
    Base Pydantic schema for Bare-Metal MCU RPC Payloads.
    Provides strict serialization to dense binary C-structs.
    """
    METHOD_ID: ClassVar[int]
    STRUCT_FORMAT: ClassVar[str]

    def to_bytes(self) -> bytes:
        values = [getattr(self, field) for field in self.__class__.model_fields.keys()]
        return struct.pack(self.STRUCT_FORMAT, *values)

    @classmethod
    def from_bytes(cls: Type[TMessage], data: bytes) -> TMessage:
        expected_size = struct.calcsize(cls.STRUCT_FORMAT)
        if expected_size != len(data):
            raise ValueError(f"Payload size mismatch for {cls.__name__}. Expected {expected_size} bytes, got {len(data)}")
        unpacked = struct.unpack(cls.STRUCT_FORMAT, data)
        field_names = list(cls.model_fields.keys())
        kwargs = dict(zip(field_names, unpacked))
        return cls(**kwargs)

class CobsFramer:
    @staticmethod
    def encode(data: bytes) -> bytes:
        encoded = bytearray()
        code = 1
        code_idx = 0
        encoded.append(0x00)
        for b in data:
            if b == 0:
                encoded[code_idx] = code
                code = 1
                code_idx = len(encoded)
                encoded.append(0x00)
            else:
                encoded.append(b)
                code += 1
                if code == 255:
                    encoded[code_idx] = code
                    code = 1
                    code_idx = len(encoded)
                    encoded.append(0x00)
        encoded[code_idx] = code
        encoded.append(0x00)
        return bytes(encoded)

    @staticmethod
    def decode(data: bytes) -> bytes:
        decoded = bytearray()
        i = 0
        while i < len(data):
            code = data[i]
            if code == 0:
                break
            i += 1
            for _ in range(1, code):
                if i < len(data):
                    decoded.append(data[i])
                    i += 1
            if code < 255 and i < len(data):
                decoded.append(0x00)
        return bytes(decoded)

class McuRpcClient(RpcClientBase):
    """
    Async RPC Client for bare-metal MCUs.
    Sends framed binary requests (Method ID + Payload) and awaits a framed response.
    Packet Structure: [Method ID: uint16] [Payload Length: uint16] [Payload: bytes] [Checksum: uint16]
    """
    def __init__(self, tx_func: Callable[[bytes], None], rx_func: Callable[[int], bytes], timeout_s: float = 2.0):
        self.tx_func = tx_func
        self.rx_func = rx_func
        self.timeout_s = timeout_s

    def _call_raw(self, method_id: int, payload: bytes) -> Optional[bytes]:
        
        length = len(payload)
        header = struct.pack("<HH", method_id, length)
        packet = header + payload
        checksum = sum(packet) & 0xFFFF
        packet += struct.pack("<H", checksum)
        
        framed = CobsFramer.encode(packet)
        self.tx_func(framed)
        
        response_buffer = bytearray()
        import time
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < self.timeout_s:
                chunk = self.rx_func(1)
                if not chunk:
                    import time; time.sleep(0.01)
                    continue
                response_buffer.extend(chunk)
                if chunk[0] == 0x00:
                    break
                    
        if time.perf_counter() - t0 >= self.timeout_s:
            logger.error("rpc_call_timed_out", method_id=method_id)
            return None
            
        try:
            unframed = CobsFramer.decode(bytes(response_buffer[:-1])) 
            if len(unframed) < 6:
                logger.error("rpc_response_malformed_too_short")
                return None
            rx_method, rx_len = struct.unpack("<HH", unframed[:4])
            rx_payload = unframed[4:-2]
            rx_crc = struct.unpack("<H", unframed[-2:])[0]
            
            calc_crc = sum(unframed[:-2]) & 0xFFFF
            if rx_crc != calc_crc:
                logger.error("rpc_crc_mismatch", expected=calc_crc, got=rx_crc)
                return None
            if rx_method != method_id:
                logger.error("rpc_method_mismatch", expected=method_id, got=rx_method)
                return None
            return rx_payload
        except Exception as e:
            logger.error("rpc_decode_failed", error=str(e))
            return None

    def invoke(self, request: McuRpcMessage, response_type: Type[TMessage]) -> Optional[TMessage]:
        raw_response = self._call_raw(request.METHOD_ID, request.to_bytes())
        if raw_response is None:
            return None
        try:
            return response_type.from_bytes(raw_response)
        except Exception as e:
            logger.error("rpc_pydantic_validation_failed", error=str(e))
            return None
