import asyncio
import json
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger()


async def parse_pdf(file_bytes: bytes) -> str:
    def _extract() -> str:
        try:
            from opendataloader_pdf import convert

            with tempfile.TemporaryDirectory() as tmp_dir:
                input_path = Path(tmp_dir) / "input.pdf"
                input_path.write_bytes(file_bytes)

                convert(
                    input_path=str(input_path),
                    output_dir=tmp_dir,
                    format="text",
                    quiet=True,
                )

                output_path = Path(tmp_dir) / "input.txt"
                if output_path.exists():
                    return output_path.read_text(encoding="utf-8").strip()

                # fallback: try json format
                json_path = Path(tmp_dir) / "input.json"
                if json_path.exists():
                    data = json.loads(json_path.read_text(encoding="utf-8"))
                    pages = []
                    for page in data.get("pages", []):
                        text = page.get("text", "")
                        if text:
                            pages.append(text.strip())
                    return "\n\n".join(pages)

                return ""
        except Exception as e:
            logger.error("pdf_parse_error", error=str(e))
            return ""

    return await asyncio.to_thread(_extract)
