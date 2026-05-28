import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pytest_mes_core.gui.routers import runner, telemetry

app = FastAPI(title="pytest-mes-core GUI Server", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For development. In prod, lock this down or serve statics directly
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runner.router)
app.include_router(telemetry.router)

@app.get("/api/health")
def health_check():
    return {"status": "ok"}

def main():
    """CLI entrypoint for running the GUI server."""
    uvicorn.run("pytest_mes_core.gui.server:app", host="0.0.0.0", port=8000, reload=False)

if __name__ == "__main__":
    main()
