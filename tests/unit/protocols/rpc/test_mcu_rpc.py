import anyio
import functools
import pytest
import struct
from unittest.mock import MagicMock
from pytest_mes_core.protocols.rpc.mcu import McuRpcMessage, CobsFramer, McuRpcClient

class DummyMcuRequest(McuRpcMessage):
    METHOD_ID = 16
    STRUCT_FORMAT = '<B'
    val: int

class DummyMcuResponse(McuRpcMessage):
    METHOD_ID = 16
    STRUCT_FORMAT = '<B H'
    val: int
    res: int

def test_mcu_rpc_message_serialization():
    req = DummyMcuRequest(val=5)
    binary = req.to_bytes()
    assert binary == b'\x05'
    resp = DummyMcuResponse.from_bytes(b'\x05\x00\x10')
    assert resp.val == 5
    assert resp.res == 4096

def test_cobs_encoding_decoding():
    raw_data = b'\x11"\x003D\x00U'
    encoded = CobsFramer.encode(raw_data)
    assert encoded[-1] == 0
    assert 0 not in encoded[:-1]
    decoded = CobsFramer.decode(encoded[:-1])
    assert decoded == raw_data

@pytest.mark.anyio
async def test_mcu_rpc_client_invoke():
    tx_mock = MagicMock()
    payload = b'\x05\x00\x10'
    header = struct.pack('<HH', 16, len(payload))
    packet = header + payload
    checksum = sum(packet) & 65535
    packet += struct.pack('<H', checksum)
    framed = CobsFramer.encode(packet)
    rx_iter = iter(framed)

    def rx_mock(size):
        try:
            return bytes([next(rx_iter)])
        except StopIteration:
            return b''
    client = McuRpcClient(tx_func=tx_mock, rx_func=rx_mock)
    req = DummyMcuRequest(val=5)
    resp = client.invoke(req, DummyMcuResponse)
    assert resp is not None
    assert resp.val == 5
    assert resp.res == 4096
    tx_mock.assert_called_once()