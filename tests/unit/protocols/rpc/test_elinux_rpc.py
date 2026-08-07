import anyio
import pytest
from unittest.mock import MagicMock
from pydantic import BaseModel
from pytest_mes_core.protocols.rpc.elinux import ELinuxJsonRpcClient

class DummyLinuxRequest(BaseModel):
    action: str
    target: str

class DummyLinuxResponse(BaseModel):
    status: int
    message: str

@pytest.mark.anyio
async def test_elinux_json_rpc_invoke():
    tx_mock = MagicMock()
    json_resp = b'{"status": 200, "message": "OK"}\n'
    rx_iter = iter(json_resp)

    def rx_mock(size):
        try:
            return bytes([next(rx_iter)])
        except StopIteration:
            return b''
    client = ELinuxJsonRpcClient(tx_func=tx_mock, rx_func=rx_mock)
    req = DummyLinuxRequest(action='reboot', target='eth0')
    resp = client.invoke(req, DummyLinuxResponse)
    assert resp is not None
    assert resp.status == 200
    assert resp.message == 'OK'
    tx_args = tx_mock.call_args[0][0]
    assert tx_args.endswith(b'\n')
    assert b'"action":"reboot"' in tx_args