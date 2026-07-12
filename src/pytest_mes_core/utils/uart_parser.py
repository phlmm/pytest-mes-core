import re
import threading
from typing import Generator

# The ultimate regex to strip ALL ANSI / VT100 terminal escape sequences (colors, cursor moves)
ANSI_ESCAPE_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

class UartStreamParser:
    """
    Centralized UART ingest engine.
    Handles fragmented byte chunks, strips ANSI color codes, normalizes line endings,
    and yields perfectly clean strings for the framework to assert against.

    Thread-safe: the RX daemon thread calls ``ingest()`` concurrently with
    consumer threads calling ``extract_lines()`` / ``clear_buffer()`` / ``buffer``.
    All buffer read-modify-write sequences are guarded by ``_lock``.
    """
    def __init__(self):
        self._buffer = ""
        self._lock = threading.Lock()

    def ingest(self, raw_bytes: bytes) -> None:
        """Decodes raw bytes, sanitizes ANSI, and appends to the internal buffer.

        Args:
            raw_bytes: Raw byte string received from the UART.
        """
        if not raw_bytes:
            return

        decoded = raw_bytes.decode('utf-8', errors='replace')
        clean_text = ANSI_ESCAPE_REGEX.sub('', decoded)
        with self._lock:
            self._buffer += clean_text

    @property
    def buffer(self) -> str:
        """Returns the current unbroken string buffer.

        Returns:
            str: The current string buffer contents.
        """
        with self._lock:
            return self._buffer

    def clear_buffer(self) -> None:
        """Purges the internal buffer."""
        with self._lock:
            self._buffer = ""

    def extract_lines(self) -> Generator[str, None, None]:
        """
        Yields complete lines as they arrive, safely removing them from the buffer.
        Leaves partial lines (like a shell prompt) safely in the buffer.

        The buffer split happens atomically under ``_lock``; the lock is NOT
        held while yielding (yielding from inside a lock across a generator's
        suspension points would risk deadlocking a concurrent ``ingest()``).
        """
        with self._lock:
            # Normalize carriage returns
            self._buffer = self._buffer.replace('\r\n', '\n')
            if '\n' in self._buffer:
                complete, self._buffer = self._buffer.rsplit('\n', 1)
                lines = complete.split('\n')
            else:
                lines = []

        for line in lines:
            clean_line = line.strip()
            if clean_line:
                yield clean_line
