"""Regression specifications for bounded HTTP decoding; execution is deferred."""
import asyncio
from contextlib import asynccontextmanager
import gzip
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient
from kiro_agent_sdk.mcp_content_encoding import McpContentDecoder, OUTPUT_CHUNK_BYTES
from kiro_agent_sdk.remote_mcp import _ResponseLimiter
from kiro_agent_sdk.remote_mcp import RemoteMcpBridge
import test_remote_mcp_toggle as fixtures


def decode(encoding, chunks):
    decoder = McpContentDecoder(encoding)
    output = b"".join(piece for chunk in chunks for piece in decoder.feed(chunk))
    decoder.finish()
    return output


def raw_deflate(data):
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(data) + compressor.flush()


class ContentDecoderTests(unittest.TestCase):
    def test_gzip_deflate_and_identity_preserve_unicode_across_tiny_chunks(self):
        text = ('{"value":"한글 🌍"}' * 100).encode()
        for encoding, data in (("gzip", gzip.compress(text)), ("deflate", zlib.compress(text)), ("identity", text)):
            self.assertEqual(decode(encoding, [data[i:i + 1] for i in range(len(data))]), text)

    def test_output_allocations_are_bounded_and_large_expansion_hits_decoded_limit(self):
        body = b"x" * (3 * OUTPUT_CHUNK_BYTES)
        compressed = gzip.compress(body)
        decoder = McpContentDecoder("gzip")
        pieces = list(decoder.feed(compressed))
        decoder.finish()
        self.assertTrue(all(len(piece) <= OUTPUT_CHUNK_BYTES for piece in pieces))
        self.assertEqual(b"".join(pieces), body)
        with patch("kiro_agent_sdk.remote_mcp._MAX_REMOTE_MESSAGE", 1024):
            limiter = _ResponseLimiter(sse=False)
            with self.assertRaisesRegex(ValueError, "message limit"):
                for part in McpContentDecoder("gzip").feed(compressed):
                    limiter.feed(part)

    def test_gzip_members_and_sse_event_boundaries_are_preserved(self):
        events = [b"data: one\r\n\r\n", b"data: two\n\n", b": heartbeat\r\r"]
        compressed = b"".join(gzip.compress(value) for value in events)
        for split in range(len(compressed) + 1):
            self.assertEqual(decode("gzip", [compressed[:split], compressed[split:]]), b"".join(events))
        with patch("kiro_agent_sdk.remote_mcp._MAX_REMOTE_MESSAGE", 16):
            limiter = _ResponseLimiter(sse=True)
            decoder = McpContentDecoder("gzip")
            for piece in decoder.feed(compressed):
                limiter.feed(piece)
            decoder.finish()

    def test_integrity_truncation_trailing_data_and_unsupported_encodings_fail(self):
        valid = gzip.compress(b"data")
        bad_crc = bytearray(valid)
        bad_crc[-8] ^= 1
        for encoding, value in (("gzip", valid[:-1]), ("gzip", bytes(bad_crc)), ("gzip", b"not gzip"),
                                ("gzip", valid + b"garbage"), ("gzip", valid + gzip.compress(b"more")[:-1]),
                                ("deflate", zlib.compress(b"data")[:-1]),
                                ("deflate", zlib.compress(b"data") + b"garbage"),
                                ("deflate", raw_deflate(b"data"))):
            with self.subTest(encoding=encoding, value=value), self.assertRaises(ValueError):
                decode(encoding, [value])
        for encoding in ("br", "zstd", "gzip, deflate", "gzip, gzip", "identity, gzip"):
            with self.assertRaises(ValueError):
                McpContentDecoder(encoding)

    def test_empty_members_offer_checkpoints_and_finished_decoder_rejects_more_data(self):
        decoder = McpContentDecoder("gzip")
        pieces = list(decoder.feed(gzip.compress(b"") * 20))
        self.assertGreaterEqual(len(pieces), 20)
        self.assertEqual(b"".join(pieces), b"")
        decoder.finish()
        with self.assertRaises(ValueError):
            list(decoder.feed(gzip.compress(b"later")))
        truncated = McpContentDecoder("gzip")
        list(truncated.feed(gzip.compress(b"value")[:-1]))
        for _ in range(2):
            with self.assertRaises(ValueError):
                truncated.finish()
        with self.assertRaises(ValueError):
            list(truncated.feed(b"\0"))


@unittest.skipUnless(fixtures.mcp is not None, "requires the optional MCP transport")
class CompressedRelayTests(unittest.IsolatedAsyncioTestCase):
    _connect = fixtures.RemoteMcpToggleTests._connect
    _send = fixtures.RemoteMcpToggleTests._send
    _request = fixtures.RemoteMcpToggleTests._request

    async def exercise_response(self, data, headers, *, guard=None, code=200, accept="application/json"):
        from mcp.client import streamable_http
        http = getattr(streamable_http, "httpx2", None) or streamable_http.httpx
        captured, raw_streams = [], []
        class Raw(http.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield data
            async def aclose(self):
                self.closed = True
        class Channel:
            async def aclose(self):
                pass
        @asynccontextmanager
        async def transport(url, *, http_client, **_):
            captured.append(http_client)
            yield Channel(), Channel(), lambda: None
        async def handle(request):
            raw = Raw()
            raw_streams.append(raw)
            self.assertEqual(request.headers["accept-encoding"], "gzip, deflate, identity")
            return http.Response(code, headers=headers, stream=raw)
        bridge = RemoteMcpBridge({"type": "http", "url": "https://fixture.example/mcp"}, request_guard=guard)
        try:
            with patch.object(http, "AsyncHTTPTransport", side_effect=lambda **_: http.MockTransport(handle)), \
                    patch.object(streamable_http, "streamable_http_client", transport):
                try:
                    async with bridge._remote_streams():
                        response = await captured[0].get("https://fixture.example/mcp", headers={"Accept": accept})
                        return response
                finally:
                    self.assertTrue(all(raw.closed for raw in raw_streams))
        finally:
            await bridge.close()

    async def test_transport_decodes_once_removes_encoded_length_and_handles_bodyless_responses(self):
        body = b'{"ok":true}'
        encoded = gzip.compress(body)
        response = await self.exercise_response(encoded,
            {"Content-Type": "application/json", "Content-Encoding": "gzip", "Content-Length": str(len(encoded))})
        self.assertEqual(response.json(), {"ok": True})
        self.assertNotIn("content-encoding", response.headers)
        self.assertNotIn("content-length", response.headers)
        empty = await self.exercise_response(b"", {"Content-Encoding": "gzip", "Content-Length": "999"}, code=204)
        self.assertEqual(empty.content, b"")

    async def test_decompression_rechecks_authorization_and_closes_failed_response(self):
        checks = 0
        async def guard():
            nonlocal checks
            checks += 1
            return checks < 3
        with self.assertRaises(PermissionError):
            await self.exercise_response(gzip.compress(b"x" * (3 * OUTPUT_CHUNK_BYTES)),
                {"Content-Type": "application/json", "Content-Encoding": "gzip"}, guard=guard)
        self.assertEqual(checks, 3)

    async def test_wire_limit_and_unsupported_encoding_close_the_owned_response(self):
        with patch("kiro_agent_sdk.remote_mcp._MAX_REMOTE_MESSAGE", 32):
            with self.assertRaisesRegex(ValueError, "Encoded"):
                await self.exercise_response(gzip.compress(bytes(range(256))),
                    {"Content-Type": "application/json", "Content-Encoding": "gzip"})
        with self.assertRaises(ValueError):
            await self.exercise_response(b"unsupported", {"Content-Encoding": "br"})

    async def test_mislabeled_sse_cannot_bypass_buffered_response_limit(self):
        body = gzip.compress(b"\n\n" * 1000)
        with patch("kiro_agent_sdk.remote_mcp._MAX_REMOTE_MESSAGE", 1024):
            for content_type, accept in (("text/event-stream", "application/json"),
                                         ("text/event-stream-fake", "text/event-stream")):
                with self.assertRaisesRegex(ValueError, "message limit"):
                    await self.exercise_response(body, {"Content-Encoding": "gzip", "Content-Type": content_type},
                                                 accept=accept)

    async def test_cancellation_interrupts_expanding_sse_without_another_network_read(self):
        checks = 0
        async def guard():
            nonlocal checks
            checks += 1
            if checks == 2:
                asyncio.current_task().cancel()
            return True
        reading = asyncio.create_task(self.exercise_response(
            gzip.compress(b": heartbeat\n\n" * 100000),
            {"Content-Type": "text/event-stream", "Content-Encoding": "gzip"}, guard=guard,
            accept="text/event-stream"))
        with self.assertRaises(asyncio.CancelledError):
            await reading
        self.assertGreaterEqual(checks, 2)

    async def test_http_and_sse_compression_keep_authentication_and_tool_toggles(self):
        for kind in ("http", "sse"):
            for encoding in ("gzip", "deflate"):
                with self.subTest(kind=kind, encoding=encoding):
                    service = fixtures.TestMcpHttpServer(kind)
                    service.response_encoding = encoding
                    service.tool_result_text = "한글 " * 100
                    url = await service.start()
                    try:
                        with tempfile.TemporaryDirectory() as temp:
                            options = KiroAgentOptions(
                                cwd=Path(temp), cli_path=fixtures.FAKE, agent_engine="v3",
                                env={"KIRO_HOME": str(Path(temp) / "kiro")}, sdk_mcp_toggle_mode="sdk-all",
                                mcp_servers={"probe": {"type": kind, "url": url,
                                                     "headers": {"Authorization": "Bearer fixture-secret"}}})
                            async with KiroSDKClient(options) as client:
                                reader, writer = await self._connect(client._mcp_servers[0])
                                try:
                                    init = await self._request(reader, writer, 1, "initialize", {
                                        "protocolVersion": "2025-03-26", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "1"}})
                                    self.assertEqual(init["result"]["serverInfo"]["name"], "fixture")
                                    await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                                    listed = await self._request(reader, writer, 2, "tools/list")
                                    self.assertEqual(listed["result"]["tools"][0]["name"], "probe")
                                    reply = await self._request(reader, writer, 3, "tools/call", {"name": "probe", "arguments": {}})
                                    self.assertEqual(reply["result"]["content"][0]["text"], service.tool_result_text)
                                    await client.toggle_mcp_server("probe", False)
                                    denied = await self._request(reader, writer, 4, "tools/call", {"name": "probe", "arguments": {}})
                                    self.assertEqual(denied["error"]["code"], -32800)
                                    self.assertEqual(service.calls, ["probe"])
                                    self.assertFalse(service.bad_auth)
                                finally:
                                    writer.close()
                                    await writer.wait_closed()
                    finally:
                        await service.close()

    async def test_compressed_oversized_message_cannot_reach_the_client(self):
        for kind in ("http", "sse"):
            service = fixtures.TestMcpHttpServer(kind)
            service.response_encoding = "gzip"
            url = await service.start()
            try:
                options = KiroAgentOptions(cli_path=fixtures.FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                    mcp_servers={"probe": {"type": kind, "url": url,
                                         "headers": {"Authorization": "Bearer fixture-secret"}}})
                with patch("kiro_agent_sdk.remote_mcp._MAX_REMOTE_MESSAGE", 1024):
                    async with KiroSDKClient(options) as client:
                        reader, writer = await self._connect(client._mcp_servers[0])
                        try:
                            await self._request(reader, writer, 1, "initialize", {
                                "protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}})
                            await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                            service.oversized = True
                            await self._send(writer, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                            try:
                                line = await asyncio.wait_for(reader.readline(), 8)
                            except (ConnectionError, asyncio.IncompleteReadError):
                                line = b""
                            if line:
                                self.assertNotIn("result", json.loads(line))
                        finally:
                            writer.close()
                            await writer.wait_closed()
            finally:
                await service.close()
