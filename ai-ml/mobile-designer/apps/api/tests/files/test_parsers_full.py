import pytest

from src.files.parsers import parse_file
from src.files.models import FileType


class TestParseFile:
    @pytest.mark.asyncio
    async def test_parse_text_file(self) -> None:
        content = b"Hello world\nLine 2"
        result = await parse_file(content, FileType.TEXT, "readme.txt")
        assert result == "Hello world\nLine 2"

    @pytest.mark.asyncio
    async def test_parse_markdown_file(self) -> None:
        content = b"# Title\n\nParagraph"
        result = await parse_file(content, FileType.MARKDOWN, "doc.md")
        assert "Title" in result

    @pytest.mark.asyncio
    async def test_parse_image_returns_empty(self) -> None:
        result = await parse_file(b"\x89PNG\r\n", FileType.IMAGE, "pic.png")
        assert result == ""

    @pytest.mark.asyncio
    async def test_parse_pdf_with_invalid_bytes(self) -> None:
        result = await parse_file(b"not a real pdf", FileType.PDF, "fake.pdf")
        # Should not crash, may return empty or partial
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_parse_docx_with_invalid_bytes(self) -> None:
        result = await parse_file(b"not a docx file", FileType.DOCX, "fake.docx")
        assert isinstance(result, str)
