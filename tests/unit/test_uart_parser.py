import threading
import time
import pytest
from pytest_mes_core.utils.uart_parser import UartStreamParser

def test_uart_stream_parser_ingests_and_decodes_clean_text():
    parser = UartStreamParser()
    parser.ingest(b'Hello World')
    assert parser.buffer == 'Hello World'

def test_uart_stream_parser_strips_ansi_codes():
    parser = UartStreamParser()
    parser.ingest(b'\x1b[32mSuccess\x1b[0m')
    assert parser.buffer == 'Success'

def test_uart_stream_parser_handles_fragmented_lines():
    parser = UartStreamParser()
    parser.ingest(b'Line 1\r\nPartial ')
    lines = list(parser.extract_lines())
    assert lines == ['Line 1']
    assert parser.buffer == 'Partial '
    parser.ingest(b'Line\r\n')
    lines = list(parser.extract_lines())
    assert lines == ['Partial Line']
    assert parser.buffer == ''

def test_uart_stream_parser_clear_buffer():
    parser = UartStreamParser()
    parser.ingest(b'Some junk')
    parser.clear_buffer()
    assert parser.buffer == ''

def test_uart_stream_parser_empty_ingest():
    parser = UartStreamParser()
    parser.ingest(b'')
    assert parser.buffer == ''

def test_uart_stream_parser_thread_safety_no_lost_lines():
    """Fix 4: one thread ingests fragments while the main thread repeatedly
    drains extract_lines() concurrently. Every numbered line must be
    extracted exactly once — a chunk ingested mid-extract must never be
    silently lost (the old unlocked read-split-reassign could drop it)."""
    parser = UartStreamParser()
    total_lines = 500
    stop_ingesting = threading.Event()

    def ingest_worker():
        for i in range(total_lines):
            line = f'LINE_{i}'
            for j in range(0, len(line), 3):
                parser.ingest(line[j:j + 3].encode())
                time.sleep(0.0001)
            parser.ingest(b'\n')
        stop_ingesting.set()
    collected = []
    t = threading.Thread(target=ingest_worker, daemon=True)
    t.start()
    deadline = time.perf_counter() + 30.0
    while not (stop_ingesting.is_set() and (not t.is_alive())) and time.perf_counter() < deadline:
        collected.extend(parser.extract_lines())
        time.sleep(0.0005)
    t.join(timeout=5.0)
    collected.extend(parser.extract_lines())
    expected = [f'LINE_{i}' for i in range(total_lines)]
    assert sorted(collected, key=lambda s: int(s.split('_')[1])) == expected
    assert len(collected) == total_lines