"""Incremental HTTP decompression with bounded allocations and strict endings."""
from __future__ import annotations

import zlib
from typing import Iterator, NoReturn

INPUT_CHUNK_BYTES = 64 * 1024
OUTPUT_CHUNK_BYTES = 64 * 1024
ACCEPT_ENCODING = "gzip, deflate, identity"


class McpContentDecoder:
    """Decode a single supported Content-Encoding; empty yields are checkpoints."""

    def __init__(self, encoding: str) -> None:
        if type(encoding) is not str:
            raise ValueError("Invalid remote MCP content encoding")
        encoding = encoding.strip().lower()
        if encoding not in ("", "identity", "gzip", "deflate"):
            raise ValueError("Unsupported remote MCP content encoding")
        self.encoding = encoding
        self.compressed = encoding in ("gzip", "deflate")
        self.decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if self.compressed else None
        self.finished = False
        self.failed = False

    def _fail(self, message: str) -> NoReturn:
        self.failed = True
        raise ValueError(message)

    def feed(self, data: bytes) -> Iterator[bytes]:
        if self.finished or self.failed:
            raise ValueError("Remote MCP response decoder has already finished")
        if type(data) is not bytes:
            self._fail("Invalid remote MCP response chunk")
        for offset in range(0, len(data), INPUT_CHUNK_BYTES):
            pending = data[offset:offset + INPUT_CHUNK_BYTES]
            if not self.compressed:
                yield pending
                continue
            drain = False
            while pending or drain:
                if self.decoder.eof:
                    if self.encoding != "gzip":
                        self._fail("Trailing data after remote MCP deflate response")
                    # RFC 1952 allows concatenated gzip members. Do not silently
                    # discard unused_data (which could hide another JSON object).
                    self.decoder = zlib.decompressobj(31)
                before = len(pending)
                try:
                    output = self.decoder.decompress(pending, OUTPUT_CHUNK_BYTES)
                except zlib.error:
                    self.failed = True
                    raise ValueError("Invalid compressed remote MCP response") from None
                pending = self.decoder.unused_data if self.decoder.eof else self.decoder.unconsumed_tail
                drain = not self.decoder.eof and len(output) == OUTPUT_CHUNK_BYTES
                if pending and len(pending) >= before and not output:
                    self._fail("Remote MCP decompression made no progress")
                # Even headers/empty members yield a scheduling checkpoint, so
                # a caller can authorize and cancel between bounded CPU steps.
                yield output

    def finish(self) -> None:
        if self.failed:
            raise ValueError("Remote MCP response decoder failed")
        if self.finished:
            return
        self.finished = True
        # flush() can allocate an unbounded buffer. All pending output is drained
        # by max_length-limited decompress calls; EOF also checks gzip CRC/trailer.
        if self.compressed and not self.decoder.eof:
            self._fail("Truncated compressed remote MCP response")
