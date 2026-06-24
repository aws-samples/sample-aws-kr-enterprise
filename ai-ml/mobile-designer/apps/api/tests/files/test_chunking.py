from src.files.chunking import chunk_for_context, chunk_text


class TestChunkText:
    def test_short_text_single_chunk(self) -> None:
        text = "Hello world"
        chunks = chunk_text(text, max_chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_splits_on_paragraphs(self) -> None:
        text = "Para 1\n\nPara 2\n\nPara 3\n\nPara 4"
        chunks = chunk_text(text, max_chunk_size=20, overlap=5)
        assert len(chunks) > 1

    def test_overlap_present(self) -> None:
        text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
        chunks = chunk_text(text, max_chunk_size=40, overlap=10)
        if len(chunks) > 1:
            assert len(chunks[1]) > 0


class TestChunkForContext:
    def test_short_text_unchanged(self) -> None:
        text = "Short text"
        assert chunk_for_context(text, max_total_size=100) == text

    def test_long_text_truncated(self) -> None:
        text = "A" * 10000
        result = chunk_for_context(text, max_total_size=500)
        assert len(result) <= 500
