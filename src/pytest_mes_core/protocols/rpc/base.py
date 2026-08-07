from typing import Optional, Protocol, Type, TypeVar
from pydantic import BaseModel

TResponse = TypeVar('TResponse', bound=BaseModel)

class RpcClientBase(Protocol):
    """
    Common base contract for all Remote Procedure Call clients in pytest-mes-core.
    Regardless of the target (MCU vs ELinux) or transport (UART vs TCP), the
    developer API remains strictly-typed using Pydantic models.
    """
    def invoke(self, request: BaseModel, response_type: Type[TResponse]) -> Optional[TResponse]:
        """
        Executes a remote procedure call.
        
        Args:
            request: The instantiated Pydantic model representing the RPC arguments.
            response_type: The Pydantic model class to deserialize the response into.
            
        Returns:
            The validated response_type instance, or None if the call timed out or failed validation.
        """
        ...
