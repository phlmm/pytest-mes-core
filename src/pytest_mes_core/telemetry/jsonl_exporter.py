import anyio
import structlog
import os
import time
import logging
from pathlib import Path
from datetime import datetime
try:
    import fcntl
except ImportError:
    fcntl = None
from pytest_mes_core.telemetry.base import StationContext, TestRecord, TelemetryDeliveryError, TelemetrySerializationError
logger = structlog.get_logger('mes_core.telemetry.jsonl')

class JsonlTelemetryExporter:
    """
    Local Disk Telemetry Sink (Grafana/Promtail compatible).
    Atomically appends minified records to disk to survive hard crashes.
    Enforces strict OS-level locks and hardware syncs.
    """

    def __init__(self, base_log_dir: Path):
        self.base_log_dir = Path(base_log_dir)
        self.active_file: Path | None = None
        self._context: StationContext | None = None

    @property
    def context(self) -> StationContext | None:
        return self._context

    def start_session(self, context: StationContext) -> None:
        """Initializes the session and dynamically generates the file path."""
        self._context = context
        date_str = datetime.now().strftime('%Y-%m-%d')
        session_dir = self.base_log_dir / 'jsonl_streams' / date_str
        session_dir.mkdir(parents=True, exist_ok=True)
        self.active_file = session_dir / f'{context.run_id}.jsonl'
        logger.info('session_armed_streaming_localized_jsonl_to_active_file', active_file=self.active_file)

    async def async_start_session(self, context: StationContext) -> None:
        return await anyio.to_thread.run_sync(self.start_session, context)

    def emit_record(self, record: TestRecord) -> None:
        """Serializes and flushes a single payload to the active file."""
        if not self.active_file:
            err_msg = 'Attempted to emit record before starting telemetry session.'
            logger.critical('fatal_err_msg', err_msg=err_msg)
            raise TelemetryDeliveryError(err_msg)
        try:
            record.sanitize()
            payload_str = record.model_dump_json(exclude_none=True) + '\n'
        except Exception as e:
            logger.critical('=' * 60)
            logger.critical('fatal_pydantic_serialization_failure')
            logger.critical('failed_to_encode_record_for_test_test_name', test_name=record.test_name)
            logger.critical('exception_e', e=e)
            logger.critical('=' * 60)
            raise TelemetrySerializationError(f'Failed to serialize record for {record.test_name}')
        logger.debug('tx_flushing_record_test_name_to_ssd', test_name=record.test_name)
        try:
            self._atomic_append(self.active_file, payload_str)
        except OSError as e:
            logger.critical('=' * 60)
            logger.critical('fatal_host_pc_disk_write_failed')
            logger.critical('target_active_file', active_file=self.active_file)
            logger.critical('is_the_factory_pc_hard_drive_full_is_the_ssd_dead_read_only')
            logger.critical('os_error_e', e=e)
            logger.critical('=' * 60)
            self._execute_emergency_dump(payload_str)
            raise TelemetryDeliveryError('Telemetry flush failed! Disk full? Emergency dump attempted.')

    async def async_emit_record(self, record: TestRecord) -> None:
        return await anyio.to_thread.run_sync(self.emit_record, record)

    def end_session(self, session_passed: bool) -> None:
        """Finalizes the run. (JSONL does not require EOF markers, so we just log it)."""
        logger.info('machine_stream_finalized_overall_result_val', val='PASS' if session_passed else 'FAIL')

    async def async_end_session(self, session_passed: bool) -> None:
        return await anyio.to_thread.run_sync(self.end_session, session_passed)

    def _atomic_append(self, filepath: Path, payload: str) -> None:
        """Writes data to the physical silicon with extreme paranoia."""
        with open(filepath, 'a', encoding='utf-8') as f:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            finally:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def _execute_emergency_dump(self, payload: str) -> None:
        """Attempts to save data to the volatile RAM disk if the main drive drops.

        Args:
            payload: The string payload to salvage.
        """
        fallback_file = Path(f'/tmp/mes_emergency_dump_{int(time.time())}_{os.getpid()}.jsonl')
        logger.critical('executing_ram_disk_emergency_dump_to_fallback_file', fallback_file=fallback_file)
        try:
            with open(fallback_file, 'a', encoding='utf-8') as fb:
                fb.write(payload)
                fb.flush()
                os.fsync(fb.fileno())
            logger.critical('emergency_dump_successful_data_survived_in_ram')
        except OSError as e:
            logger.critical('=' * 60)
            logger.critical('fatal_total_host_pc_catastrophe')
            logger.critical('emergency_ram_disk_dump_failed_e', e=e)
            logger.critical('raw_payload_salvage')
            logger.critical(payload.strip())
            logger.critical('=' * 60)