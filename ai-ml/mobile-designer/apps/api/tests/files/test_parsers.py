import pytest

from src.files.parsers.text_parser import parse_text


class TestTextParser:
    def test_parses_utf8(self) -> None:
        content = "Hello 세계".encode("utf-8")
        result = parse_text(content)
        assert result == "Hello 세계"

    def test_handles_invalid_utf8(self) -> None:
        content = b"\xff\xfe invalid bytes"
        result = parse_text(content)
        assert "invalid bytes" in result

    def test_empty_bytes(self) -> None:
        result = parse_text(b"")
        assert result == ""
