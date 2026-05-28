import asyncio
import threading
import queue
import customtkinter as ctk
from pytest_mes_core.gui.controller import MesTestController

# Enforce dark mode for industrial look
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

class MesDesktopApp(ctk.CTk):
    def __init__(self, async_loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.async_loop = async_loop
        self.controller = MesTestController()
        
        # Bridge queue from async world to sync UI world
        self.log_queue = queue.Queue()
        
        self.title("pytest-mes-core | Hardware Validation")
        self.geometry("900x600")
        
        # UI Layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1)
        
        # Left Pane: Controls
        self.control_frame = ctk.CTkFrame(self, corner_radius=10)
        self.control_frame.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        
        self.title_label = ctk.CTkLabel(self.control_frame, text="pytest-mes-core", font=ctk.CTkFont(size=20, weight="bold"))
        self.title_label.pack(pady=20, padx=10)
        
        self.op_label = ctk.CTkLabel(self.control_frame, text="Operator ID")
        self.op_label.pack(pady=5, padx=10, anchor="w")
        
        self.op_entry = ctk.CTkEntry(self.control_frame, placeholder_text="Scan badge...")
        self.op_entry.insert(0, "OPERATOR-01")
        self.op_entry.pack(pady=5, padx=10, fill="x")
        
        self.start_btn = ctk.CTkButton(self.control_frame, text="START TEST", command=self.handle_start, height=50)
        self.start_btn.pack(pady=20, padx=10, fill="x")
        
        self.stop_btn = ctk.CTkButton(self.control_frame, text="E-STOP", command=self.handle_stop, height=50, fg_color="red", hover_color="darkred")
        self.stop_btn.pack(pady=5, padx=10, fill="x")
        self.stop_btn.configure(state="disabled")

        self.status_label = ctk.CTkLabel(self.control_frame, text="Status: Idle", text_color="gray")
        self.status_label.pack(side="bottom", pady=20)
        
        # Right Pane: Telemetry
        self.log_frame = ctk.CTkFrame(self, corner_radius=10)
        self.log_frame.grid(row=0, column=1, padx=10, pady=10, sticky="nsew")
        self.log_frame.grid_rowconfigure(0, weight=1)
        self.log_frame.grid_columnconfigure(0, weight=1)
        
        self.textbox = ctk.CTkTextbox(self.log_frame, font=ctk.CTkFont(family="Courier", size=12))
        self.textbox.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        
        # Start background async tasks
        asyncio.run_coroutine_threadsafe(self.bridge_telemetry(), self.async_loop)
        
        # Start polling the thread-safe queue for UI updates
        self.after(100, self.poll_logs)

    async def bridge_telemetry(self):
        """Runs in async thread: pulls from Controller's async queue, pushes to thread-safe queue."""
        q = self.controller.subscribe_telemetry()
        try:
            while True:
                line = await q.get()
                self.log_queue.put(line)
        finally:
            self.controller.unsubscribe_telemetry(q)

    def poll_logs(self):
        """Runs in UI thread: pulls from thread-safe queue, updates textbox."""
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            self.textbox.insert("end", line + "\n")
            self.textbox.see("end")
            
            # Simple status polling since we know the test finished if the subprocess completes
            if self.controller.is_running:
                self.status_label.configure(text="Status: Testing in progress...", text_color="green")
            else:
                self.status_label.configure(text="Status: Idle", text_color="gray")
                self.start_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")

        self.after(50, self.poll_logs)

    def handle_start(self):
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_label.configure(text="Status: Starting...", text_color="orange")
        
        # Schedule the async start_test to run in the background event loop
        fut = asyncio.run_coroutine_threadsafe(
            self.controller.start_test("tests/", self.op_entry.get()), 
            self.async_loop
        )
        # We don't block the UI waiting for it. The poll_logs will detect if it started or failed.

    def handle_stop(self):
        self.status_label.configure(text="Status: Stopping...", text_color="orange")
        asyncio.run_coroutine_threadsafe(self.controller.stop_test(), self.async_loop)

def run_asyncio_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def main():
    # 1. Create a background asyncio event loop
    loop = asyncio.new_event_loop()
    async_thread = threading.Thread(target=run_asyncio_loop, args=(loop,), daemon=True)
    async_thread.start()
    
    # 2. Start the native UI
    app = MesDesktopApp(loop)
    app.mainloop()
    
    # Cleanup
    loop.call_soon_threadsafe(loop.stop)
    async_thread.join()

if __name__ == "__main__":
    main()
