import structlog
import time
import re
import uuid
import logging
import serial
import threading
import queue
from contextlib import contextmanager
from typing import Generator, Optional, Any, AsyncGenerator
import anyio
from pytest_mes_core.config import HostSerialConfig
from pytest_mes_core.transports.base import CommandResult, TransportConnectionError, TransportTimeoutError
from pytest_mes_core.utils.uart_parser import UartStreamParser
from pytest_mes_core.transports.watchdog import UartKernelWatchdog

logger = structlog.get_logger('mes_core.transports.serial')

class EphemeralSerialClient:
    """
    Unified Pub/Sub Multiplexer Engine for UART.
    Liskov-compliant with DutTransport. Provides thread-safe TX locking
    and broadcasts RX byte-streams to multiple asynchronous subscribers
    (Watchdog, Tests, RPC Clients).
    """
    ANSI_ESCAPE_B = re.compile(b'\x1b\\[[0-9;]*[a-zA-Z]')
    KERNEL_LOG_PATTERN = re.compile('^\\[\\s*\\d+\\.\\d+\\]\\s*')

    def __init__(self, cfg: HostSerialConfig):
        self.cfg = cfg
        self.ser: Optional[serial.Serial] = None
        self._is_locked = False
        
        self._tx_lock = threading.RLock()
        self._subscribers = []
        self._sub_lock = threading.Lock()
        self._stop_rx_event = threading.Event()
        self._rx_thread = None

        self._default_raw_queue = queue.Queue()
        self._subscribers.append(self._default_raw_queue)

        self.parser = UartStreamParser()
        self.watchdog = UartKernelWatchdog(self)

    def start_rx_daemon(self):
        if self._rx_thread and self._rx_thread.is_alive():
            return
        self._stop_rx_event.clear()
        self._rx_thread = threading.Thread(target=self._rx_daemon_loop, daemon=True)
        self._rx_thread.start()

    def stop_rx_daemon(self):
        self._stop_rx_event.set()
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=1.0)
            
    def subscribe(self, maxsize=1024) -> queue.Queue:
        q = queue.Queue(maxsize=maxsize)
        with self._sub_lock:
            self._subscribers.append(q)
        return q
        
    def unsubscribe(self, q: queue.Queue):
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _rx_daemon_loop(self):
        """Background pub/sub multiplexer thread."""
        while not self._stop_rx_event.is_set():
            if self.ser and self.ser.is_open:
                try:
                    chunk = b''
                    with self._sub_lock:
                        if self.ser.in_waiting > 0:
                            chunk = self.ser.read(max(1, self.ser.in_waiting))
                            if chunk:
                                self.parser.ingest(chunk)
                                for q in self._subscribers:
                                    try:
                                        q.put_nowait(chunk)
                                    except queue.Full:
                                        pass
                    if not chunk:
                        time.sleep(0.01)
                except Exception:
                    time.sleep(0.05)
            else:
                time.sleep(0.05)

    def connect(self) -> None:
        try:
            self.ser = serial.Serial(port=self.cfg.port, baudrate=self.cfg.baudrate, timeout=0.1, exclusive=True)
            self.ser.reset_output_buffer()
            self.flush_buffers()
            logger.debug('bound_to_port_and_flushed_stale_os_buffers_tx_rx', port=self.cfg.port)
            self.start_rx_daemon()
            self.watchdog.start()
        except serial.SerialException as e:
            err_str = str(e).lower()
            if 'device or resource busy' in err_str or 'access is denied' in err_str:
                from pytest_mes_core.host_adapters.diagnostics import ResourceDiagnostics
                owner = ResourceDiagnostics.get_device_owner(self.cfg.port)
                logger.critical('=' * 60)
                if owner:
                    error_msg = f'Serial port {self.cfg.port} is locked by PID/Process: {owner}!'
                    logger.critical('fatal_error_msg', error_msg=error_msg)
                    logger.critical('[UART] Please close the competing application (minicom, Putty) and retry.')
                else:
                    error_msg = f'Serial port {self.cfg.port} is busy (OS refused to identify owner).'
                    logger.critical('fatal_error_msg', error_msg=error_msg)
                logger.critical('=' * 60)
                raise TransportConnectionError(error_msg)
            raise TransportConnectionError(f'Failed to bind Host UART {self.cfg.port}: {e}')

    def disconnect(self) -> None:
        self.stop_rx_daemon()
        self.watchdog.stop()
        if self.ser and self.ser.is_open:
            try:
                self.ser.reset_output_buffer()
                self.ser.reset_input_buffer()
            except Exception:
                pass
            self.ser.close()

    async def async_connect(self) -> None:
        import anyio
        await anyio.to_thread.run_sync(self.connect)

    async def async_disconnect(self) -> None:
        import anyio
        await anyio.to_thread.run_sync(self.disconnect)

    @property
    def is_connected(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    def expect(self, pattern: str, timeout_s: float=5.0, blast_char: str='', active_redraw: bool=True) -> str:
        if not self.is_connected or self.ser is None:
            raise TransportConnectionError('Serial port is closed.')
        
        pattern_bytes = pattern.encode('utf-8')
        
        q = self.subscribe(maxsize=0)
        try:
            with self._tx_lock:
                if blast_char:
                    blast_bytes = blast_char.encode('utf-8')
                    for _ in range(3):
                        self.ser.write(blast_bytes)
                        time.sleep(0.05)
                    self.ser.flush()

            logger.debug('expecting_pattern_timeout_timeout_s_s_active_redraw_active_redraw', pattern=pattern, timeout_s=timeout_s, active_redraw=active_redraw)
            t_end = time.perf_counter() + timeout_s
            last_rx_time = time.perf_counter()
            raw_buffer = bytearray()
            
            while time.perf_counter() < t_end:
                try:
                    chunk = q.get(timeout=0.01)
                    raw_buffer.extend(chunk)
                    clean_buffer = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)
                    if pattern_bytes in clean_buffer:
                        return clean_buffer.decode('utf-8', errors='replace')
                    
                    last_rx_time = time.perf_counter()
                except queue.Empty:
                    pass
                
                if active_redraw and time.perf_counter() - last_rx_time > 2.0:
                    with self._tx_lock:
                        logger.debug('[UART] Console silent. Injecting ping to redraw prompt...')
                        self.ser.write(b'\n')
                        self.ser.flush()
                    last_rx_time = time.perf_counter()
                    
        finally:
            self.unsubscribe(q)

        dump = self.ANSI_ESCAPE_B.sub(b'', raw_buffer)[-200:].decode('utf-8', errors='replace').strip()
        logger.error('timeout_expecting_pattern_buffer_yielded_dump', pattern=pattern, dump=dump)
        raise TransportTimeoutError(f"UART Expect Timeout: '{pattern}' not found.")

    async def async_expect(self, pattern: str, timeout_s: float=5.0, blast_char: str='', active_redraw: bool=True) -> str:
        import anyio
        from functools import partial
        return await anyio.to_thread.run_sync(
            partial(self.expect, pattern, timeout_s=timeout_s, blast_char=blast_char, active_redraw=active_redraw)
        )

    def write_line(self, cmd: str, sensitive: bool=False) -> None:
        if self.ser is None:
            raise RuntimeError('Cannot write while UART is closed.')
        with self._tx_lock:
            if sensitive:
                logger.debug("[UART] TX -> '********'")
            else:
                log_cmd = cmd if len(cmd) < 256 else cmd[:253] + '...'
                logger.debug('tx_log_cmd', log_cmd=log_cmd)
            payload = f'{cmd}\n'.encode('utf-8')
            for i in range(0, len(payload), 16):
                self.ser.write(payload[i:i + 16])
                self.ser.flush()
                time.sleep(0.002)

    def safe_run(self, cmd: str, timeout_s: float=30.0, check_exit_code: bool=False, auto_retry: bool=False, **kwargs: Any) -> CommandResult:
        expected_prompt = kwargs.get('expected_prompt', getattr(self.cfg, 'os_shell_prompt', '~#'))
        if not self.is_connected or self.ser is None:
            raise TransportConnectionError('Serial port is closed.')
        with self._tx_lock:
            self.flush_buffers()
            t0 = time.perf_counter()
            if not cmd.strip():
                self.write_line('')
                try:
                    self.expect(expected_prompt, timeout_s=1.0)
                except TransportTimeoutError:
                    pass
                duration = round(time.perf_counter() - t0, 3)
                return CommandResult(command=cmd, stdout='', stderr='', exited=0, ok=True, duration_s=duration)
            is_uboot = any((p in expected_prompt for p in ['=>', 'U-Boot', 'barebox', 'Verdin']))
            if not is_uboot:
                exec_token = uuid.uuid4().hex[:8]
                magic_marker = f'__MES_EXIT_{exec_token}__'
                start_marker = f'__MES_START_{exec_token}__'
                safe_cmd = cmd.replace("'", "'\\''")
                injected_cmd = f"printf '\\n{start_marker}\\n' ; sh -c '{safe_cmd}' ; printf '\\n{magic_marker}:%d\\n' $?"
            else:
                injected_cmd = cmd
            self.ser.write(b'\x03')
            self.ser.flush()
            try:
                self.expect(expected_prompt, timeout_s=0.5)
            except TransportTimeoutError:
                pass
            self.flush_buffers()
            self.write_line(injected_cmd)
            try:
                raw_output = self.expect(expected_prompt, timeout_s)
                duration = round(time.perf_counter() - t0, 3)
                stdout = raw_output
                exited = -1 if is_uboot else 0
                if not is_uboot:
                    exit_match = re.search(f'{magic_marker}:(\\d+)', stdout)
                    if exit_match:
                        exited = int(exit_match.group(1))
                    else:
                        exited = -2
                    end_idx = stdout.rfind(magic_marker)
                    if end_idx != -1:
                        stdout = stdout[:end_idx]
                    else:
                        stdout = stdout.replace(magic_marker, '')
                    start_idx = stdout.rfind(start_marker)
                    if start_idx != -1:
                        stdout = stdout[start_idx + len(start_marker):]
                clean_lines = []
                for line in stdout.split('\n'):
                    clean = line.strip()
                    if not clean or expected_prompt in clean:
                        continue
                    if self.KERNEL_LOG_PATTERN.search(clean):
                        logger.debug('suppressed_async_kernel_log_clean', clean=clean)
                        continue
                    clean_lines.append(clean)
                stdout_clean = '\n'.join(clean_lines).strip()
                if is_uboot and ('Unknown command' in stdout_clean or 'Error' in stdout_clean):
                    exited = 1
                elif is_uboot:
                    exited = 0
                result = CommandResult(command=cmd, stdout=stdout_clean, stderr='', exited=exited, ok=exited == 0, duration_s=duration)
                if check_exit_code and (not result.ok):
                    raise RuntimeError(f"UART Command '{cmd}' failed with exit code {result.exited}:\n{result.stdout}")
                return result
            except TransportTimeoutError as e:
                duration = round(time.perf_counter() - t0, 3)
                logger.warning('execution_timed_out_after_timeout_s_s_cmd', timeout_s=timeout_s, cmd=cmd)
                result = CommandResult(command=cmd, stdout=self.live_buffer, stderr=str(e), exited=-1, ok=False, duration_s=duration)
                if check_exit_code:
                    raise RuntimeError(f"UART Command '{cmd}' timed out after {timeout_s}s")
                return result

    async def async_safe_run(self, cmd: str, timeout_s: float=30.0, check_exit_code: bool=False, auto_retry: bool=False, **kwargs: Any) -> CommandResult:
        import anyio
        from functools import partial
        return await anyio.to_thread.run_sync(
            partial(self.safe_run, cmd, timeout_s=timeout_s, check_exit_code=check_exit_code, auto_retry=auto_retry, **kwargs)
        )

    def flush_buffers(self) -> None:
        with self._sub_lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.reset_output_buffer()
                    self.ser.reset_input_buffer()
                except Exception:
                    pass
            self.parser.clear_buffer()
            for q in self._subscribers:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break

    async def async_flush_buffers(self) -> None:
        import anyio
        await anyio.to_thread.run_sync(self.flush_buffers)

    def raw_write(self, data: bytes) -> None:
        with self._tx_lock:
            if self.ser and self.ser.is_open:
                self.ser.write(data)
                self.ser.flush()

    def raw_read_chunk(self) -> bytes:
        try:
            return self._default_raw_queue.get(timeout=0.05)
        except queue.Empty:
            return b''

    def raw_read(self, size: int) -> bytes:
        q = self.subscribe(maxsize=0)
        try:
            buf = bytearray()
            t_end = time.perf_counter() + 5.0
            while time.perf_counter() < t_end:
                try:
                    chunk = q.get(timeout=0.01)
                    buf.extend(chunk)
                    if len(buf) >= size:
                        return bytes(buf[:size])
                except queue.Empty:
                    pass
            return bytes(buf)
        finally:
            self.unsubscribe(q)

    async def async_raw_read_chunk(self) -> bytes:
        import anyio
        return await anyio.to_thread.run_sync(self.raw_read_chunk)

    async def async_raw_read(self, size: int) -> bytes:
        import anyio
        return await anyio.to_thread.run_sync(self.raw_read, size)

    async def async_raw_write(self, data: bytes) -> None:
        import anyio
        await anyio.to_thread.run_sync(self.raw_write, data)

    def raw_set_timeout(self, timeout: float) -> None:
        if self.ser and self.ser.is_open:
            self.ser.timeout = timeout

    def read_clean_stream(self, filter_kernel: bool=True) -> Generator[str, None, None]:
        if not self.ser or not self.ser.is_open:
            return
        for line in self.parser.extract_lines():
            if filter_kernel and self.KERNEL_LOG_PATTERN.match(line):
                continue
            yield line

    @property
    def live_buffer(self) -> str:
        return self.parser.buffer
