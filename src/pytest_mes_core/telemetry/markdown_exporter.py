import anyio
import structlog
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
from pytest_mes_core.telemetry.base import StationContext, TestRecord
logger = structlog.get_logger('mes_core.telemetry.markdown')

class DeveloperMarkdownExporter:
    """
    Developer / Bring-up Data Sink.
    Generates a rich Markdown file optimized for IDEs, formatting tracebacks and FSM context.
    """

    def __init__(self, base_log_dir: Path):
        self.base_log_dir = base_log_dir
        self.filepath: Optional[Path] = None
        self.total_duration = 0.0
        self._context: Optional[StationContext] = None

    @property
    def context(self) -> Optional[StationContext]:
        return self._context

    def start_session(self, context: StationContext) -> None:
        """Initializes the session and dynamically generates the file path.

        Args:
            context: The station metadata context for this session.
        """
        self._context = context
        date_str = datetime.now().strftime('%Y-%m-%d')
        time_str = datetime.now().strftime('%H-%M-%S')
        report_dir = self.base_log_dir / 'bringup_reports' / date_str
        report_dir.mkdir(parents=True, exist_ok=True)
        self.filepath = report_dir / f'Bringup_{time_str}_SN-{context.dut_serial}_{context.run_id}.md'
        hw_sn = context.dut_manifest.get('HW_SN_CARRIER', '') if context.dut_manifest else ''
        hw_sn_line = f'  \n**PCB HW SN:** `{hw_sn}`' if hw_sn else ''
        header = f'# MES Bring-up Report\n**Run ID:** `{context.run_id}`  \n**Jig ID:** `{context.jig_id}` | **Operator:** `{context.operator_id}`  \n**DUT Serial:** `{context.dut_serial}` | **FW Version:** `{context.firmware_version}`{hw_sn_line}\n\n---\n\n'
        with open(self.filepath, 'w', encoding='utf-8') as f:
            f.write(header)

    async def async_start_session(self, context, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.start_session, context, *args, **kwargs)

    def emit_record(self, record: TestRecord) -> None:
        """Serializes and flushes a single payload to the active markdown file.

        Args:
            record: The test record to emit.
        """
        if not self.filepath:
            return
        self.total_duration += record.duration_s
        status_icon = '✅ PASS' if record.passed else '❌ FAIL'
        retry_tag = f' *(Retry {record.iteration})*' if record.context.get('is_retry') else ''
        md = f'## {status_icon}: `{record.test_name}`{retry_tag}\n'
        md += f'- **Duration:** {record.duration_s}s\n'
        md += f"- **FSM State:** `{record.context.get('fsm_state', 'UNKNOWN')}`\n\n"
        if not record.passed and record.error_message:
            md += f'### Error: {record.error_message}\n'
            full_trace = record.context.get('full_traceback')
            if full_trace:
                md += '<details><summary><b>View Full Traceback</b></summary>\n\n'
                md += f'```python\n{full_trace}\n```\n\n</details>\n\n'
            post_mortem = record.context.get('post_mortem')
            if post_mortem:
                md += '### Hardware Post-Mortem Dump\n'
                for cmd, output in post_mortem.items():
                    md += f'**Command:** `{cmd}`\n```bash\n{output}\n```\n'
        if record.metrics:
            md += '### Physical Metrics\n'
            md += '| Metric | Value |\n|---|---|\n'
            for k, v in record.metrics.items():
                md += f'| `{k}` | **{v}** |\n'
            md += '\n'
        custom_ctx = {k: v for k, v in record.context.items() if k not in ['is_retry', 'fsm_state', 'full_traceback', 'post_mortem', 'stress_loop_iteration']}
        if custom_ctx:
            md += '<details><summary><i>Execution Context</i></summary>\n\n```json\n'
            md += json.dumps(custom_ctx, indent=2, default=str)
            md += '\n```\n</details>\n\n'
        md += '---\n\n'
        with open(self.filepath, 'a', encoding='utf-8') as f:
            f.write(md)

    async def async_emit_record(self, record, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.emit_record, record, *args, **kwargs)

    def end_session(self, session_passed: bool) -> None:
        """Finalizes the session and renames the file with the final status.

        Args:
            session_passed: True if all tests passed, False otherwise.
        """
        if not self.filepath:
            return
        footer = f"## Session Complete\n- **Final Status:** {('✅ SUCCESS' if session_passed else '❌ FAILED')}\n- **Total Test Execution Time:** {round(self.total_duration, 2)}s\n\n"
        if self._context and self._context.dut_manifest:
            footer += '## Hardware Manifest (Station BOM)\n'
            footer += '| Component | Identifier |\n|---|---|\n'
            # Pin the three identity rows first so the order is always deterministic
            board_sn = self._context.dut_manifest.get('evse_carrier_serial', '')
            som_sn = self._context.dut_manifest.get('serial_number', self._context.dut_serial)
            hw_sn_val = self._context.dut_manifest.get('HW_SN_CARRIER', '')
            if board_sn:
                footer += f'| `Board Serial` | **{board_sn}** |\n'
            footer += f'| `SOM Serial` | **{som_sn}** |\n'
            if hw_sn_val:
                footer += f'| `HW SN` | **{hw_sn_val}** |\n'
            _pinned = {'evse_carrier_serial', 'serial_number', 'HW_SN_CARRIER', 'custom_flags', 'software_manifest'}
            for k, v in self._context.dut_manifest.items():
                # Skip nested dicts (e.g. a software_manifest that slipped through),
                # blank values, and the pinned rows already rendered above.
                if v and not isinstance(v, dict) and k not in _pinned:
                    pretty_key = k.replace('_', ' ').title()
                    footer += f'| `{pretty_key}` | **{v}** |\n'
            footer += '\n'
        if self._context and getattr(self._context, 'software_manifest', None):
            for component_name, component_data in self._context.software_manifest.items():
                if not isinstance(component_data, dict):
                    component_data = {component_name: component_data}
                    component_name = 'System'
                footer += f'## Software Build Version: {component_name}\n'
                footer += '| Key | Value |\n|---|---|\n'
                for k, v in component_data.items():
                    if v:
                        footer += f'| `{k}` | **{v}** |\n'
                footer += '\n'
        with open(self.filepath, 'a', encoding='utf-8') as f:
            f.write(footer)
        try:
            status = 'PASS' if session_passed else 'FAIL'
            time_str = datetime.now().strftime('%H-%M-%S')
            run_id = self._context.run_id if self._context else 'UNKNOWN_RUN'
            safe_operator = self._context.operator_id.replace('/', '_') if self._context else 'UNKNOWN'
            serial = self._context.dut_serial if self._context else 'PENDING'
            hw_sn = self._context.dut_manifest.get('HW_SN_CARRIER', '') if (self._context and self._context.dut_manifest) else ''
            hw_sn_part = f'_HW-{hw_sn}' if hw_sn else ''
            final_name = f'{status}_{time_str}_{safe_operator}_SN-{serial}{hw_sn_part}_{run_id}.md'
            final_path = self.filepath.parent / final_name
            self.filepath.rename(final_path)
            self.filepath = final_path
            logger.debug('bringup_report_finalized_and_renamed_to_final_name', final_name=final_name)
        except Exception as e:
            logger.error('failed_to_rename_bringup_report_e', e=e)
    async def async_end_session(self, session_passed, *args, **kwargs):
        return await anyio.to_thread.run_sync(self.end_session, session_passed, *args, **kwargs)
