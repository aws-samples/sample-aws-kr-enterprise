from src.files.models import FileType
from src.files.parsers.docx_parser import parse_docx
from src.files.parsers.pdf_parser import parse_pdf
from src.files.parsers.text_parser import parse_text


async def parse_file(file_bytes: bytes, file_type: FileType, filename: str) -> str:
    match file_type:
        case FileType.PDF:
            return await parse_pdf(file_bytes)
        case FileType.DOCX:
            return await parse_docx(file_bytes)
        case FileType.MARKDOWN | FileType.TEXT:
            return parse_text(file_bytes)
        case _:
            return ""
