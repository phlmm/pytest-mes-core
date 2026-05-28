import asyncio
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from pytest_mes_core.gui.controller import MesTestController

router = APIRouter(prefix="/api", tags=["Runner"])
controller = MesTestController()

class RunRequest(BaseModel):
    project_path: str = "tests/"
    operator_id: str = "OPERATOR-01"
    env_config: Optional[str] = None

@router.post("/run")
async def run_test(req: RunRequest):
    try:
        pid = await controller.start_test(req.project_path, req.operator_id, req.env_config)
        return {"status": "started", "pid": pid}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stop")
async def stop_test():
    stopped = await controller.stop_test()
    if stopped:
        return {"status": "stopping"}
    return {"status": "not_running_or_already_stopped"}

@router.get("/status")
async def get_status():
    return {
        "is_running": controller.is_running,
        "pid": controller.process.pid if controller.process else None
    }
