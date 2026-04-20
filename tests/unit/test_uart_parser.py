import pytest
from pytest_mes_core.utils.uart_parser import UartStreamParser

def test_uart_stream_parser_ingests_and_decodes_clean_text():
    parser = UartStreamParser()
    parser.ingest(b"Hello World")
    assert parser.buffer == "Hello World"

def test_uart_stream_parser_strips_ansi_codes():
    parser = UartStreamParser()
    # Green colored text followed by normal text
    parser.ingest(b"\x1b[32mSuccess\x1b[0m")
    assert parser.buffer == "Success"

def test_uart_stream_parser_handles_fragmented_lines():
    parser = UartStreamParser()
    parser.ingest(b"Line 1\r\nPartial ")
    
    lines = list(parser.extract_lines())
    assert lines == ["Line 1"]
    
    # The partial line should remain in the buffer
    assert parser.buffer == "Partial "
    
    # Complete the partial line
    parser.ingest(b"Line\r\n")
    lines = list(parser.extract_lines())
    assert lines == ["Partial Line"]
    assert parser.buffer == ""

def test_uart_stream_parser_clear_buffer():
    parser = UartStreamParser()
    parser.ingest(b"Some junk")
    parser.clear_buffer()
    assert parser.buffer == ""

def test_uart_stream_parser_empty_ingest():
    parser = UartStreamParser()
    parser.ingest(b"")
    assert parser.buffer == ""
