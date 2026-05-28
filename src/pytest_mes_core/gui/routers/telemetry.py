import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pytest_mes_core.gui.controller import MesTestController

router = APIRouter(prefix="/ws", tags=["Telemetry"])
controller = MesTestController()

@router.websocket("/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    await websocket.accept()
    queue = controller.subscribe_telemetry()
    try:
        while True:
            # Wait for logs from the runner
            log_line = await queue.get()
            await websocket.send_text(log_line)
    except WebSocketDisconnect:
        pass
    finally:
        controller.unsubscribe_telemetry(queue)

