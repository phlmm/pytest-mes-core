import structlog
import threading
from functools import partial
from typing import Any
from pytest_mes_core.transports.base import DutTransport, CommandResult, TransportConnectionError
logger = structlog.get_logger('mes_core.transports.failover')

class FailoverTransport(DutTransport):
    """
    Reactive Failover Matrix.
    Wraps two transports. If the primary shatters, it permanently shifts
    to the secondary for the remainder of its lifecycle.

    Explicitly implements the DutTransport protocol to enable proper static
    type checking across the framework.
    """

    def __init__(self, primary: DutTransport, fallback: DutTransport):
        self.primary = primary
        self.fallback = fallback
        self.is_failed_over = False
        self._recovery_thread = None
        self._stop_recovery = threading.Event()
        self._connect_lock = threading.Lock()  # prevents double thread spawn on concurrent connect()
        if hasattr(self.fallback, 'watchdog') and getattr(self.fallback, 'watchdog', None):
            watchdog = self.fallback.watchdog
            if hasattr(watchdog, 'register_panic_callback'):
                watchdog.register_panic_callback(self._on_panic)

    def _probe_primary_recovery(self) -> None:
        """Background thread that periodically checks if the primary transport has recovered.
        
        If the primary transport reconnects successfully, this automatically fails back
        to the high-speed link.
        """
        while not self._stop_recovery.is_set():
            if self.is_failed_over:
                try:
                    if not self.primary.is_connected:
                        self.primary.connect()
                    res = self.primary.safe_run('echo MES_PING', timeout_s=2.0)
                    if res.ok and 'MES_PING' in res.stdout:
                        logger.info('[Router] HIGH-SPEED RECOVERY: Primary transport recovered! Failing-back.')
                        self.is_failed_over = False
                except Exception:
                    pass
            self._stop_recovery.wait(timeout=5.0)

    def _on_panic(self) -> None:
        """Callback triggered by the watchdog when an asynchronous kernel panic is detected.
        
        Actively severs the primary SSH connection to force an immediate failover to the
        serial console for forensic extraction.
        """
        logger.critical('[Router] Watchdog detected panic! Severing Primary connection to fail fast...')
        if self.primary.is_connected:
            self.primary.disconnect()

    @property
    def is_connected(self) -> bool:
        return self.fallback.is_connected if self.is_failed_over else self.primary.is_connected

    def _start_recovery_thread(self) -> None:
        """Arms the background recovery-probe thread, guarded against double-spawn.

        Must be called while holding (or having just released, per the async
        caller's brief-lock pattern) ``_connect_lock`` semantics: it re-checks
        thread liveness under the lock so two concurrent connect() calls never
        each spawn their own recovery thread.
        """
        with self._connect_lock:
            if self._recovery_thread and self._recovery_thread.is_alive():
                return
            self._stop_recovery.clear()
            self._recovery_thread = threading.Thread(target=self._probe_primary_recovery, daemon=True)
            self._recovery_thread.start()

    def connect(self) -> None:
        """Connects both primary and fallback transports and arms the failover matrix.

        Thread-safe: guarded by ``_connect_lock`` to prevent a race where two
        concurrent callers each pass the ``is_connected`` check and both spawn
        a recovery thread.
        """
        armed = False
        with self._connect_lock:
            if not self.is_connected:
                armed = True
                logger.info('[Router] Arming dual-transport failover matrix...')

                # Connect the reliable fallback (UART) first so it is available immediately
                self.fallback.connect()

                # Attempt to connect the high-speed primary (SSH), but gracefully accept failure
                # If the board is in BOOTLOADER or POWER_OFF, this will naturally fail.
                try:
                    self.primary.connect()
                    self.is_failed_over = False
                except TransportConnectionError:
                    logger.warning('[Router] Primary transport offline during setup. Matrix starting in FAILOVER mode.')
                    self.is_failed_over = True

        # _start_recovery_thread() re-acquires _connect_lock itself, so it must be
        # called after the lock above is released (threading.Lock is not reentrant).
        if armed:
            self._start_recovery_thread()
            logger.debug('[Router] Dual-transport routing matrix armed.')

    def disconnect(self) -> None:
        """Tears down the dual-transport matrix and stops the recovery thread.

        Ensures zero-leakage by cleanly disconnecting both primary and fallback
        transports, and joining the recovery thread.
        """
        self._stop_recovery.set()
        if self._recovery_thread and self._recovery_thread.is_alive():
            self._recovery_thread.join(timeout=1.0)
        logger.debug('[Router] ZERO-LEAKAGE: Tearing down dual-transport matrix.')
        self.primary.disconnect()
        self.fallback.disconnect()



    def safe_run(self, cmd: str, timeout_s: float=30.0, check_exit_code: bool=False, auto_retry: bool=False, **kwargs: Any) -> CommandResult:
        """Executes a command on the target, failing over to the fallback transport if necessary.

        Args:
            cmd: The shell command to execute on the target.
            timeout_s: Maximum time in seconds to wait for command completion.
            check_exit_code: If True, raises an exception if the command exits with a non-zero status.
            auto_retry: If True, retries the command on the fallback transport if the primary fails.
            **kwargs: Additional keyword arguments passed to the underlying transport's safe_run.

        Returns:
            CommandResult: The result of the executed command, containing stdout, stderr, and exit code.

        Raises:
            TransportConnectionError: If the primary transport fails and the command is not marked for auto_retry.
        """
        if self.is_failed_over:
            logger.debug('[Router] Routing via Fallback Transport...')
            return self.fallback.safe_run(cmd, timeout_s, check_exit_code, auto_retry, **kwargs)
        try:
            return self.primary.safe_run(cmd, timeout_s, check_exit_code, auto_retry, **kwargs)
        except TransportConnectionError as e:
            logger.critical('=' * 60)
            logger.critical('fatal_primary_transport_severed_e', e=e)
            logger.critical('[Router] ENGAGING OUT-OF-BAND HARDWARE FALLBACK...')
            logger.critical('=' * 60)
            self.is_failed_over = True
            logger.debug('[Router] Transmitting wake-up pulse to fallback console...')
            self.fallback.safe_run('\n', timeout_s=1.0, check_exit_code=False)
            if auto_retry:
                logger.info('hardware_failover_successful_retrying_idempotent_command_cmd', cmd=cmd)
                return self.fallback.safe_run(cmd, timeout_s, check_exit_code, auto_retry, **kwargs)
            else:
                logger.warning('failover_successful_but_command_cmd_lacks_auto_retry_true_escalating_failure_to_fsm', cmd=cmd)
                raise


    # ==========================================
    # PUB/SUB & OUT-OF-BAND UART PASSTHROUGH
    # ==========================================
    # These methods provide immortal access to the Fallback Transport (UART)
    # regardless of the Primary Transport's (SSH) current state.

    def subscribe(self, maxsize: int = 1024):
        """Pass-through to Fallback Transport's Pub/Sub subscribe."""
        if hasattr(self.fallback, 'subscribe'):
            return self.fallback.subscribe(maxsize)
        raise NotImplementedError("Fallback transport does not support subscribe().")

    def unsubscribe(self, q) -> None:
        """Pass-through to Fallback Transport's Pub/Sub unsubscribe."""
        if hasattr(self.fallback, 'unsubscribe'):
            return self.fallback.unsubscribe(q)
        raise NotImplementedError("Fallback transport does not support unsubscribe().")

    def expect(self, pattern: str, timeout_s: float = 5.0, blast_char: str = '', active_redraw: bool = True) -> str:
        """Pass-through to Fallback Transport's expect()."""
        if hasattr(self.fallback, 'expect'):
            return self.fallback.expect(pattern, timeout_s=timeout_s, blast_char=blast_char, active_redraw=active_redraw)
        raise NotImplementedError("Fallback transport does not support expect().")


    def write_line(self, cmd: str, sensitive: bool = False) -> None:
        """Pass-through to Fallback Transport's write_line()."""
        if hasattr(self.fallback, 'write_line'):
            return self.fallback.write_line(cmd, sensitive=sensitive)
        raise NotImplementedError("Fallback transport does not support write_line().")

    def raw_write(self, data: bytes) -> None:
        """Pass-through to Fallback Transport's raw_write()."""
        if hasattr(self.fallback, 'raw_write'):
            return self.fallback.raw_write(data)
        raise NotImplementedError("Fallback transport does not support raw_write().")

    def raw_read_chunk(self) -> bytes:
        """Pass-through to Fallback Transport's raw_read_chunk().

        .. deprecated::
            Use :meth:`subscribe` / :meth:`unsubscribe` instead.
            ``raw_read_chunk()`` bypasses the pub/sub multiplexer so concurrent
            consumers (watchdog, FSM) will silently miss any bytes consumed here.
        """
        import warnings
        warnings.warn(
            "FailoverTransport.raw_read_chunk() bypasses the pub/sub multiplexer. "
            "Use subscribe()/unsubscribe() for multiplexed byte access.",
            DeprecationWarning,
            stacklevel=2,
        )
        if hasattr(self.fallback, 'raw_read_chunk'):
            return self.fallback.raw_read_chunk()
        raise NotImplementedError("Fallback transport does not support raw_read_chunk().")

    def raw_read(self, size: int) -> bytes:
        """Pass-through to Fallback Transport's raw_read()."""
        if hasattr(self.fallback, 'raw_read'):
            return self.fallback.raw_read(size)
        raise NotImplementedError("Fallback transport does not support raw_read().")

    def flush_buffers(self) -> None:
        """Pass-through to Fallback Transport's flush_buffers()."""
        if hasattr(self.fallback, 'flush_buffers'):
            return self.fallback.flush_buffers()
        raise NotImplementedError("Fallback transport does not support flush_buffers().")







    def read_clean_stream(self):
        """Pass-through to Fallback Transport's read_clean_stream().

        .. deprecated::
            Use :class:`UartEventStream` instead.
            ``read_clean_stream()`` bypasses the pub/sub multiplexer — concurrent
            consumers miss all bytes consumed here, and events are not dispatched
            to the EventBus.
        """
        import warnings
        warnings.warn(
            "FailoverTransport.read_clean_stream() bypasses the pub/sub multiplexer. "
            "Use UartEventStream.open() for event-driven, multiplexed UART access.",
            DeprecationWarning,
            stacklevel=2,
        )
        if hasattr(self.fallback, 'read_clean_stream'):
            return self.fallback.read_clean_stream()
        raise NotImplementedError("Fallback transport does not support read_clean_stream().")