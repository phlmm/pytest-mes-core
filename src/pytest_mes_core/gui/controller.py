import asyncio
import os
import signal
from typing import Optional

class MesTestController:
    """Agnostic controller managing the pytest subprocess and telemetry queues."""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MesTestController, cls).__new__(cls)
            cls._instance.process = None
            cls._instance.is_running = False
            cls._instance.log_subscribers = []
        return cls._instance

    def __init__(self):
        # State initialized in __new__ to enforce Singleton
        pass

    async def start_test(self, project_path: str, operator_id: str, env_config: Optional[str] = None):
        if self.is_running:
            raise RuntimeError("A test is already running.")
        
        cmd = ["pytest", project_path, "-p", "mes_core", f"--operator-id={operator_id}"]
        if env_config:
            cmd.append(f"--env-config={env_config}")

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            self.is_running = True
            
            asyncio.create_task(self._read_stdout())
            asyncio.create_task(self._wait_for_process())
            
            return self.process.pid
        except Exception as e:
            self.is_running = False
            raise RuntimeError(f"Failed to start test: {e}")

    async def stop_test(self):
        if not self.is_running or not self.process:
            return False
        
        try:
            os.kill(self.process.pid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            self.is_running = False
            self.process = None
            return False

    def subscribe_telemetry(self) -> asyncio.Queue:
        queue = asyncio.Queue()
        self.log_subscribers.append(queue)
        return queue

    def unsubscribe_telemetry(self, queue: asyncio.Queue):
        if queue in self.log_subscribers:
            self.log_subscribers.remove(queue)

    async def _read_stdout(self):
        if not self.process or not self.process.stdout:
            return
        
        while self.is_running:
            line = await self.process.stdout.readline()
            if not line:
                break
            
            line_str = line.decode('utf-8').rstrip()
            for queue in self.log_subscribers:
                try:
                    queue.put_nowait(line_str)
                except asyncio.QueueFull:
                    pass

    async def _wait_for_process(self):
        if self.process:
            await self.process.wait()
            self.is_running = False
            self.process = None
