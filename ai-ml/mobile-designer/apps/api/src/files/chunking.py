import re


def chunk_text(text: str, max_chunk_size: int = 2000, overlap: int = 200) -> list[str]:
    if len(text) <= max_chunk_size:
        return [text]

    paragraphs = re.split(r"\n\n+", text)
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_size = 0

    for para in paragraphs:
        para_size = len(para)

        if current_size + para_size > max_chunk_size and current_chunk:
            chunks.append("\n\n".join(current_chunk))

            overlap_text = "\n\n".join(current_chunk)
            overlap_start = max(0, len(overlap_text) - overlap)
            overlap_content = overlap_text[overlap_start:]

            current_chunk = [overlap_content] if overlap_content.strip() else []
            current_size = len(overlap_content)

        current_chunk.append(para)
        current_size += para_size

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def chunk_for_context(text: str, max_total_size: int = 8000) -> str:
    if len(text) <= max_total_size:
        return text

    chunks = chunk_text(text, max_chunk_size=max_total_size // 2)
    result = chunks[0]

    if len(chunks) > 1:
        remaining = max_total_size - len(result) - 50
        if remaining > 0:
            tail = chunks[-1][:remaining]
            result += "\n\n[...중략...]\n\n" + tail

    return result[:max_total_size]
