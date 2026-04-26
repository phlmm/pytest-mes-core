import json
import structlog
from typing import Optional, Callable, Type, TypeVar
from pydantic import BaseModel
from .base import RpcClientBase

logger = structlog.get_logger('mes_core.protocols.rpc.elinux')

TMessage = TypeVar('TMessage', bound=BaseModel)

class ELinuxJsonRpcClient(RpcClientBase):
    """
    Async RPC Client for Embedded Linux targets.
    Communicates via newline-delimited JSON payloads over standard TCP/Unix sockets.
    
    Compatible with Cap'n Proto over JSON, FastAPI, or simple Python socket servers
    running on the i.MX8 target.
    """
    def __init__(self, tx_func: Callable[[bytes], None], rx_func: Callable[[int], bytes], timeout_s: float = 5.0):
        self.tx_func = tx_func
        self.rx_func = rx_func
        self.timeout_s = timeout_s

    async def async_invoke(self, request: BaseModel, response_type: Type[TMessage]) -> Optional[TMessage]:
        import anyio
        
        # Serialize Pydantic model to JSON and append newline delimiter
        payload_str = request.model_dump_json() + "\n"
        
        # Transmit over the socket
        await anyio.to_thread.run_sync(self.tx_func, payload_str.encode('utf-8'))
        
        response_buffer = bytearray()
        
        with anyio.move_on_after(self.timeout_s) as cancel_scope:
            while True:
                # Read until newline
                chunk = await anyio.to_thread.run_sync(self.rx_func, 1)
                if not chunk:
                    await anyio.sleep(0.01)
                    continue
                    
                response_buffer.extend(chunk)
                if chunk == b'\n':
                    break
                    
        if cancel_scope.cancel_called:
            logger.error("elinux_json_rpc_timed_out")
            return None
            
        try:
            response_str = response_buffer.decode('utf-8').strip()
            if not response_str:
                return None
            
            # Pydantic natively parses the JSON response
            return response_type.model_validate_json(response_str)
        except Exception as e:
            logger.error("elinux_json_rpc_decode_failed", error=str(e))
            return None
