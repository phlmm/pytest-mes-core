import asyncio
from unittest.mock import MagicMock
import queue

async def my_generator(mock_unsubscribe, rx_q):
    try:
        yield 1
    finally:
        mock_unsubscribe(rx_q)

async def run_test():
    mock_unsubscribe = MagicMock()
    rx_q = queue.Queue()
    gen = my_generator(mock_unsubscribe, rx_q)
    try:
        async for item in gen:
            if item == 1:
                raise ValueError("panic")
    except ValueError:
        pass
    finally:
        await gen.aclose()
        
    print("Calls:", mock_unsubscribe.call_count)

asyncio.run(run_test())
