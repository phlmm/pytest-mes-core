import threading
import functools
import time
import structlog
from typing import Dict, List, Optional
from pytest_mes_core.transports.base import DutTransport
from pytest_mes_core.instruments.power_supplies import ScpiPowerSupply

logger = structlog.get_logger('mes_core.telemetry.profiler')

class HardwareProfiler:
    """
    Continuous telemetry profiler that non-blockingly samples hardware metrics
    (PSU current/voltage, DUT temperatures, and CPU load) using the AnyIO event loop.
    
    This is designed to be used as an async context manager around stress tests
    to automatically gather peak and average physical telemetry.
    """
    def __init__(self, dut: Optional[DutTransport] = None, psu: Optional[ScpiPowerSupply] = None, interval_s: float = 1.0):
        self.dut = dut
        self.psu = psu
        self.interval_s = max(0.2, interval_s)
        self.is_running = False
        self.metrics: Dict[str, List[float]] = {
            "timestamps_s": [],
            "temp_c": [],
            "psu_current_a": [],
            "psu_voltage_v": []
        }
        self._t0: float = 0.0
        self._thread = None
        self._stop_event = threading.Event()

    def __enter__(self):
        self.is_running = True
        self._t0 = time.perf_counter()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.is_running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            
        samples = len(self.metrics["timestamps_s"])
        logger.info("async_profiling_complete", samples=samples, summary=self.summarize())

    def _poll_loop(self):
        while not self._stop_event.is_set():
            start_poll = time.perf_counter()
            self.metrics["timestamps_s"].append(round(start_poll - self._t0, 3))

            if self.psu:
                try:
                    v = self.psu.measure_voltage()
                    c = self.psu.measure_current()
                    self.metrics["psu_voltage_v"].append(v)
                    self.metrics["psu_current_a"].append(c)
                except Exception as e:
                    logger.debug("psu_poll_error", error=str(e))

            if self.dut and getattr(self.dut, 'is_connected', True):
                try:
                    # Fast-poll the thermal zone via FSM transport
                    res = self.dut.safe_run("cat /sys/class/thermal/thermal_zone0/temp", timeout_s=1.0, check_exit_code=False)
                    raw = res.stdout.strip()
                    if res.ok and raw:
                        try:
                            self.metrics["temp_c"].append(round(int(raw) / 1000.0, 2))
                        except ValueError:
                            pass  # non-numeric console noise — skip the sample
                except Exception as e:
                    logger.debug("dut_poll_error", error=str(e))

            elapsed = time.perf_counter() - start_poll
            sleep_time = max(0.01, self.interval_s - elapsed)
            
            # Explicit yield to prevent tight-loop lockup if interval is aggressively low
            self._stop_event.wait(sleep_time)

    def summarize(self) -> Dict[str, float]:
        """Returns peak/avg metrics suitable for JSONL telemetry injection."""
        summary = {}
        if self.metrics["temp_c"]:
            summary["peak_temp_c"] = max(self.metrics["temp_c"])
            summary["avg_temp_c"] = round(sum(self.metrics["temp_c"]) / len(self.metrics["temp_c"]), 2)
        if self.metrics["psu_current_a"]:
            summary["peak_current_a"] = max(self.metrics["psu_current_a"])
            summary["avg_current_a"] = round(sum(self.metrics["psu_current_a"]) / len(self.metrics["psu_current_a"]), 3)
        if self.metrics["psu_voltage_v"]:
            summary["avg_voltage_v"] = round(sum(self.metrics["psu_voltage_v"]) / len(self.metrics["psu_voltage_v"]), 3)
        return summary
