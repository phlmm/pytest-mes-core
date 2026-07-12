import anyio
import structlog
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from pytest_mes_core.telemetry.base import StationContext, TestRecord
logger = structlog.get_logger('mes_core.telemetry.receipt')

class OperatorReceiptExporter:
    """
    Human-Readable Data Sink.
    Generates a simple .txt summary with a highly visible filename (PASS_14-30_SN-123.txt).
    """

    def __init__(self, base_log_dir: Path):
        self.base_log_dir = base_log_dir
        self.total_tests = 0
        self.failed_tests = 0
        self.skipped_tests = 0
        self.start_time: Optional[float] = None
        self._context: Optional[StationContext] = None

    @property
    def context(self) -> Optional[StationContext]:
        return self._context

    def start_session(self, context: StationContext) -> None:
        """Initializes the session.

        Args:
            context: The station metadata context for this session.
        """
        self._context = context
        self.start_time = time.perf_counter()

    async def async_start_session(self, context: StationContext) -> None:
        return await anyio.to_thread.run_sync(self.start_session, context)

    def emit_record(self, record: TestRecord) -> None:
        """Updates internal statistics based on the emitted test record.

        Args:
            record: The test record to process.
        """
        self.total_tests += 1
        if record.outcome == "skipped":
            self.skipped_tests += 1
        elif record.outcome == "failed":
            self.failed_tests += 1
        elif record.outcome == "unknown" and not record.passed:
            self.failed_tests += 1

    async def async_emit_record(self, record: TestRecord) -> None:
        return await anyio.to_thread.run_sync(self.emit_record, record)

    def end_session(self, session_passed: bool) -> None:
        """Finalizes the run and writes the receipt file.

        Args:
            session_passed: True if all tests passed, False otherwise.
        """
        if not self._context:
            return
        date_str = datetime.now().strftime('%Y-%m-%d')
        receipt_dir = self.base_log_dir / 'operator_receipts' / date_str
        receipt_dir.mkdir(parents=True, exist_ok=True)
        status = 'PASS' if session_passed else 'FAIL'
        time_str = datetime.now().strftime('%H-%M-%S')
        run_id = self._context.run_id
        safe_operator = self._context.operator_id.replace('/', '_')
        serial = self._context.dut_serial
        hw_sn = self._context.dut_manifest.get('HW_SN_CARRIER', '') if self._context.dut_manifest else ''
        hw_sn_part = f'_HW-{hw_sn}' if hw_sn else ''
        filename = f'{status}_{time_str}_{safe_operator}_SN-{serial}{hw_sn_part}_{run_id}.txt'
        filepath = receipt_dir / filename
        duration = round(time.perf_counter() - self.start_time, 2) if self.start_time else 0.0
        hw_sn_line = f'PCB HW SN    : {hw_sn}\n' if hw_sn else ''
        receipt_body = f'=== EOL TEST RECEIPT ===\nRun ID       : {run_id}\nStatus       : {status}\nJig ID       : {self._context.jig_id}\nOperator     : {self._context.operator_id}\nDUT Serial   : {serial}\n{hw_sn_line}Duration     : {duration} seconds\n------------------------\nTotal Tests  : {self.total_tests}\nSkipped Tests : {self.skipped_tests}\nFailed Tests : {self.failed_tests}\n========================\n'
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(receipt_body)
        logger.info('generated_operator_receipt_name', name=filepath.name)
    async def async_end_session(self, session_passed: bool) -> None:
        return await anyio.to_thread.run_sync(self.end_session, session_passed)
