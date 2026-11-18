import asyncio
import contextlib
import gzip
import json
import os
import tempfile
import unittest
import zlib
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from kiro_agent_sdk import AgentDefinition, KiroAgentOptions, KiroSDKClient, ProtocolError
from kiro_agent_sdk.remote_mcp import RemoteMcpBridge, _ResponseLimiter

FAKE = str(Path(__file__).with_name("fake_kiro.py"))

try:
    import mcp  # noqa: F401
except ImportError:
    mcp = None


class TestMcpHttpServer:
    """Loopback-only MCP Streamable HTTP and legacy SSE fixture."""

    def __init__(self, kind, *, http_events=False, protocol_version=None):
        self.kind = kind
        self.http_events = http_events
        self.protocol_version = protocol_version
        self.block_next = False
        self.server = None
        self.connections = set()
        self.sse_writer = None
        self.sse_writers = {}
        self.next_session = 0
        self.sse_ready = asyncio.Event()
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls = []
        self.requests = []
        self.cancellations = []
        self.cancellation_sessions = []
        self.tool_sessions = []
        self.cancellation_pairs = []
        self.tool_pairs = []
        self.cancel_release_after = 1
        self.background = set()
        self.bad_auth = False
        self.deletes = 0
        self.stall_delete = False
        self.stall_sse_tool_post = False
        self.stall_sse_cancel_post = False
        self.oversized = False
        self.compressed = False
        self.response_encoding = None
        self.sse_compressors = {}
        self.tool_result_text = "remote-result"
        self.tool_name = "probe"

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}/{'mcp' if self.kind == 'http' else 'sse'}"

    async def close(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        for task in list(self.background):
            task.cancel()
        if self.connections:
            await asyncio.gather(*list(self.connections), return_exceptions=True)
        if self.background:
            await asyncio.gather(*list(self.background), return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = head.decode("ascii").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            headers = {key.lower(): value for key, value in headers.items()}
            self.bad_auth |= headers.get("authorization") != "Bearer fixture-secret"
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            if method == "GET" and (self.kind == "sse" or self.http_events):
                encoding = b""
                if self.response_encoding is not None:
                    encoding = ("Content-Encoding: " + self.response_encoding + "\r\n").encode()
                    self.sse_compressors[writer] = zlib.compressobj(
                        wbits=31 if self.response_encoding == "gzip" else 15)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n" + encoding +
                             b"Cache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
                if self.kind == "sse":
                    self.next_session += 1
                    session_id = str(self.next_session)
                    self.sse_writers[session_id] = writer
                    self.write_sse(writer, ("event: endpoint\ndata: /messages?sessionId=" + session_id + "\n\n").encode())
                await writer.drain()
                self.sse_writer = writer
                self.sse_ready.set()
                try:
                    await reader.read()
                finally:
                    if self.kind == "sse":
                        self.sse_writers.pop(session_id, None)
                return
            if method == "GET":
                await self._respond(writer, 405, b"", {})
                return
            if method == "DELETE":
                self.deletes += 1
                if self.stall_delete:
                    await self.release.wait()
                await self._respond(writer, 204, b"", {})
                return
            message = json.loads(body)
            self.requests.append(message)
            name = message.get("method")
            if name == "tools/call":
                self.calls.append(message.get("params", {}).get("name"))
                if self.kind == "sse":
                    session_id = parse_qs(urlsplit(path).query).get("sessionId", [None])[0]
                    self.tool_sessions.append(session_id)
                    self.tool_pairs.append((message.get("id"), session_id))
                if self.calls[-1] == "slow" or self.block_next:
                    self.block_next = False
                    self.started.set()
                    if self.kind == "sse":
                        async def finish_slow():
                            await self.release.wait()
                            session_id = parse_qs(urlsplit(path).query).get("sessionId", [None])[0]
                            stream = self.sse_writers.get(session_id)
                            if stream is None:
                                return
                            response = {"jsonrpc": "2.0", "id": message["id"],
                                        "result": {"content": [{"type": "text", "text": self.tool_result_text}]}}
                            try:
                                self.write_sse(stream, ("event: message\ndata: " + json.dumps(response) + "\n\n").encode())
                                await stream.drain()
                            except ConnectionError:
                                pass
                        pending = asyncio.create_task(finish_slow())
                        self.background.add(pending)
                        pending.add_done_callback(self.background.discard)
                        if self.stall_sse_tool_post:
                            await self.release.wait()
                        await self._respond(writer, 202, b"", {})
                        return
                    await self.release.wait()
                result = {"content": [{"type": "text", "text": self.tool_result_text}]}
            elif name == "notifications/cancelled":
                self.cancellations.append(message.get("params", {}).get("requestId"))
                if self.kind == "sse":
                    session_id = parse_qs(urlsplit(path).query).get("sessionId", [None])[0]
                    self.cancellation_sessions.append(session_id)
                    self.cancellation_pairs.append((message.get("params", {}).get("requestId"), session_id))
                if self.stall_sse_cancel_post:
                    await self.release.wait()
                elif len(self.cancellations) >= self.cancel_release_after:
                    self.release.set()
                result = None
            elif name == "initialize":
                result = {"protocolVersion": self.protocol_version or
                          message.get("params", {}).get("protocolVersion", "2025-03-26"),
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fixture", "version": "1"}}
            elif name == "tools/list":
                tool = {"name": self.tool_name, "inputSchema": {"type": "object"}}
                if self.oversized:
                    tool["description"] = "x" * 4096
                result = {"tools": [tool]}
            elif "id" in message and name:
                result = {}
            else:
                result = None
            response = ({"jsonrpc": "2.0", "id": message["id"], "result": result}
                        if "id" in message and name else None)
            if self.kind == "sse":
                if response:
                    await self.sse_ready.wait()
                    session_id = parse_qs(urlsplit(path).query).get("sessionId", [None])[0]
                    stream = self.sse_writers[session_id]
                    self.write_sse(stream, ("event: message\ndata: " + json.dumps(response) + "\n\n").encode())
                    await stream.drain()
                await self._respond(writer, 202, b"", {})
            elif response:
                response_headers = {"Content-Type": "application/json", "Mcp-Session-Id": "fixture"}
                if self.compressed:
                    response_headers["Content-Encoding"] = "gzip"
                await self._respond(writer, 200, json.dumps(response).encode(), response_headers)
            else:
                await self._respond(writer, 202, b"", {})
        except (ConnectionError, asyncio.CancelledError, asyncio.IncompleteReadError):
            pass
        finally:
            compressor = self.sse_compressors.pop(writer, None)
            if compressor is not None:
                with contextlib.suppress(ConnectionError, zlib.error):
                    writer.write(compressor.flush())
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.connections.discard(task)

    def write_sse(self, writer, data):
        compressor = self.sse_compressors.get(writer)
        if compressor is not None:
            data = compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH)
        writer.write(data)

    async def _respond(self, writer, code, body, headers):
        if self.response_encoding is not None and body and code != 204:
            body = gzip.compress(body) if self.response_encoding == "gzip" else zlib.compress(body)
            headers = {**headers, "Content-Encoding": self.response_encoding}
        reasons = {200: "OK", 202: "Accepted", 204: "No Content", 405: "Method Not Allowed"}
        head = f"HTTP/1.1 {code} {reasons[code]}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
        head += "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        writer.write(head.encode() + b"\r\n" + body)
        await writer.drain()


@unittest.skipUnless(mcp is not None, "remote relay requires optional mcp dependency")
class RemoteMcpToggleTests(unittest.IsolatedAsyncioTestCase):
    async def _connect(self, config):
        env = {entry["name"]: entry["value"] for entry in config["env"]}
        reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
        await writer.drain()
        return reader, writer

    async def _send(self, writer, message):
        writer.write((json.dumps(message) + "\n").encode())
        await writer.drain()

    async def _request(self, reader, writer, ident, method, params=None):
        await self._send(writer, {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
        return json.loads(await asyncio.wait_for(reader.readline(), 8))

    async def _run_transport(self, kind):
        service = TestMcpHttpServer(kind, http_events=True)
        url = await service.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                    env={"KIRO_HOME": str(root / "kiro")}, sdk_mcp_toggle_mode="sdk-all",
                    mcp_servers={"probe": {"type": kind, "url": url,
                                             "headers": {"Authorization": "Bearer fixture-secret"}}},
                    agent_mcp_reference_mode="duplicate",
                    agents={"child": AgentDefinition("Child", "Run", mcp_servers=["probe"])})
                with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                    async with KiroSDKClient(options) as client:
                        self.assertNotIn("fixture-secret", repr(client._mcp_servers))
                        self.assertNotIn("fixture-secret", repr(client._agent_mcp_bridges))
                        with self.assertRaisesRegex(ProtocolError, "OAuth"):
                            await client.reconnect_mcp_server("probe", start_oauth=True)
                        reader, writer = await self._connect(client._mcp_servers[0])
                        try:
                            init = await self._request(reader, writer, 1, "initialize", {
                                "protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}})
                            self.assertEqual(init["result"]["serverInfo"]["name"], "fixture")
                            await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                            listed = await self._request(reader, writer, 2, "tools/list")
                            self.assertEqual(listed["result"]["tools"][0]["name"], "probe")
                            await asyncio.wait_for(service.sse_ready.wait(), 8)
                            server_request = {"jsonrpc": "2.0", "id": "server-request", "method": "ping"}
                            service.sse_writer.write(("event: message\ndata: " + json.dumps(server_request) + "\n\n").encode())
                            await service.sse_writer.drain()
                            forwarded = json.loads(await asyncio.wait_for(reader.readline(), 8))
                            self.assertEqual((forwarded["id"], forwarded["method"]), ("server-request", "ping"))
                            await self._send(writer, {"jsonrpc": "2.0", "id": "server-request", "result": {}})
                            for _ in range(100):
                                if any(item.get("id") == "server-request" and "result" in item
                                       for item in service.requests):
                                    break
                                await asyncio.sleep(0.01)
                            else:
                                self.fail("Response to server-initiated request was not forwarded")
                            await client.toggle_mcp_server("probe", False)
                            denied = await self._request(reader, writer, 3, "tools/call", {"name": "probe", "arguments": {}})
                            self.assertEqual(denied["error"]["code"], -32800)
                            writer.write(b'{"jsonrpc":"2.0","id":30,"method":"tools/call","method":"ping"}\n')
                            await writer.drain()
                            self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 8))["error"]["code"], -32800)
                            self.assertEqual(service.calls, [])
                            await client.toggle_mcp_server("probe", True)
                            result = await self._request(reader, writer, 4, "tools/call", {"name": "probe", "arguments": {}})
                            self.assertEqual(result["result"]["content"][0]["text"], "remote-result")
                            self.assertEqual(service.calls, ["probe"])
                            self.assertFalse(service.bad_auth)
                        finally:
                            writer.close()
                            await writer.wait_closed()
        finally:
            await service.close()

    async def test_cross_origin_http_redirect_and_sse_endpoint_do_not_receive_headers(self):
        reached = []
        tasks = set()
        async def target(reader, writer):
            reached.append(await reader.read(1024))
            writer.close()
            await writer.wait_closed()
        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        try:
            for kind in ("http", "sse"):
                async def source(reader, writer):
                    task = asyncio.current_task()
                    tasks.add(task)
                    try:
                        await reader.readuntil(b"\r\n\r\n")
                        if kind == "http":
                            data = (f"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://127.0.0.1:{target_port}/other\r\n"
                                    "Content-Length: 0\r\nConnection: close\r\n\r\n").encode()
                        else:
                            data = ("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                                    "Connection: keep-alive\r\n\r\n"
                                    f"event: endpoint\ndata: http://127.0.0.1:{target_port}/other\n\n").encode()
                        writer.write(data)
                        await writer.drain()
                        if kind == "sse":
                            await reader.read()
                    finally:
                        writer.close()
                        await writer.wait_closed()
                        tasks.discard(task)
                source_server = await asyncio.start_server(source, "127.0.0.1", 0)
                source_port = source_server.sockets[0].getsockname()[1]
                try:
                    options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                        startup_timeout=2,
                        mcp_servers={"probe": {"type": kind, "url": f"http://127.0.0.1:{source_port}/mcp",
                            "headers": {"Authorization": "Bearer fixture-secret"}}})
                    async with KiroSDKClient(options) as client:
                        reader, writer = await self._connect(client._mcp_servers[0])
                        try:
                            await self._send(writer, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"}}})
                            line = await asyncio.wait_for(reader.readline(), 8)
                            if line:
                                self.assertIn("error", json.loads(line))
                            self.assertEqual(reached, [])
                        finally:
                            writer.close()
                            await writer.wait_closed()
                finally:
                    source_server.close()
                    await source_server.wait_closed()
            self.assertEqual(reached, [])
        finally:
            target_server.close()
            await target_server.wait_closed()
            for task in list(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)

    async def test_interrupt_requests_http_and_sse_cancellation(self):
        for kind in ("http", "sse"):
            with self.subTest(kind=kind):
                service = TestMcpHttpServer(kind, protocol_version="2025-03-26")
                service.stall_sse_tool_post = kind == "sse"
                url = await service.start()
                try:
                    options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
                        sdk_mcp_toggle_mode="sdk-all", sdk_tool_interrupt_mode="cancel-observed",
                        mcp_servers={"probe": {"type": kind, "url": url,
                                                  "headers": {"Authorization": "Bearer fixture-secret"}}})
                    async with KiroSDKClient(options) as client:
                        reader, writer = await self._connect(client._mcp_servers[0])
                        pending = None
                        try:
                            await self._request(reader, writer, 1, "initialize", {
                                "protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}})
                            await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                            await client.query("cancel")
                            pending = asyncio.create_task(self._request(
                                reader, writer, 41, "tools/call", {"name": "slow", "arguments": {}}))
                            await asyncio.wait_for(service.started.wait(), 8)
                            await asyncio.wait_for(client.interrupt(), 5)
                            for _ in range(200):
                                if service.cancellations:
                                    break
                                await asyncio.sleep(0.01)
                            self.assertEqual(service.cancellations, [41])
                            self.assertFalse(service.bad_auth)
                            response = await pending
                            self.assertEqual(response["id"], 41)
                            self.assertEqual((await self._request(
                                reader, writer, 42, "tools/call",
                                {"name": "probe", "arguments": {}}))["error"]["code"], -32800)
                            _ = [item async for item in client.receive_response()]
                            response = await self._request(reader, writer, 43, "tools/call",
                                                           {"name": "probe", "arguments": {}})
                            self.assertIn("result", response)
                        finally:
                            service.release.set()
                            if pending is not None and not pending.done():
                                pending.cancel()
                                await asyncio.gather(pending, return_exceptions=True)
                            writer.close()
                            await writer.wait_closed()
                finally:
                    await service.close()

    async def test_concurrent_remote_http_requests_preserve_ids_and_arguments(self):
        service = TestMcpHttpServer("http")
        url = await service.start()
        try:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": url,
                                         "headers": {"Authorization": "Bearer fixture-secret"}}})
            async with KiroSDKClient(options) as client:
                reader, writer = await self._connect(client._mcp_servers[0])
                try:
                    await self._request(reader, writer, 1, "initialize", {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}})
                    await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                    for ident in range(2, 22):
                        await self._send(writer, {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                                                  "params": {"name": f"probe-{ident}", "arguments": {}}})
                    responses = [json.loads(await asyncio.wait_for(reader.readline(), 8)) for _ in range(20)]
                    self.assertEqual({item["id"] for item in responses}, set(range(2, 22)))
                    self.assertEqual(set(service.calls), {f"probe-{ident}" for ident in range(2, 22)})
                finally:
                    writer.close()
                    await writer.wait_closed()
        finally:
            await service.close()

    async def test_inflight_remote_request_can_finish(self):
        for kind in ("http", "sse"):
            with self.subTest(kind=kind):
                service = TestMcpHttpServer(kind)
                url = await service.start()
                try:
                    options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                        mcp_servers={"probe": {"type": kind, "url": url,
                                                 "headers": {"Authorization": "Bearer fixture-secret"}}})
                    async with KiroSDKClient(options) as client:
                        reader, writer = await self._connect(client._mcp_servers[0])
                        try:
                            await self._request(reader, writer, 1, "initialize", {
                                "protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}})
                            await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                            await self._send(writer, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                       "params": {"name": "slow", "arguments": {}}})
                            await asyncio.wait_for(service.started.wait(), 8)
                            await client.toggle_mcp_server("probe", False)
                            denied = await self._request(reader, writer, 3, "tools/call", {"name": "probe", "arguments": {}})
                            self.assertEqual(denied["error"]["code"], -32800)
                            service.release.set()
                            result = json.loads(await asyncio.wait_for(reader.readline(), 8))
                            self.assertEqual(result["id"], 2)
                            self.assertEqual(service.calls, ["slow"])
                        finally:
                            service.release.set()
                            writer.close()
                            await writer.wait_closed()
                finally:
                    await service.close()

    async def test_explicit_cancellation_sends_mcp_notice_to_remote_server(self):
        for kind in ("http", "sse"):
            with self.subTest(kind=kind):
                service = TestMcpHttpServer(kind)
                url = await service.start()
                try:
                    options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                        mcp_servers={"probe": {"type": kind, "url": url,
                                                 "headers": {"Authorization": "Bearer fixture-secret"}}})
                    async with KiroSDKClient(options) as client:
                        reader, writer = await self._connect(client._mcp_servers[0])
                        try:
                            await self._request(reader, writer, 1, "initialize", {
                                "protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}})
                            await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                            await self._send(writer, {"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                                                       "params": {"name": "slow", "arguments": {}}})
                            await asyncio.wait_for(service.started.wait(), 8)
                            await client.request_mcp_tool_cancellation("probe")
                            for _ in range(200):
                                if service.cancellations:
                                    break
                                await asyncio.sleep(0.01)
                            self.assertEqual(service.cancellations, [41])
                            # Complete locally even if the server keeps working.
                            result = json.loads(await asyncio.wait_for(reader.readline(), 8))
                            self.assertEqual((result["id"], result["error"]["code"]), (41, -32800))
                            service.release.set()
                            response = await self._request(reader, writer, 42, "tools/list", {})
                            self.assertEqual(response["id"], 42)
                        finally:
                            service.release.set()
                            writer.close()
                            await writer.wait_closed()
                finally:
                    await service.close()

    async def test_sse_cancellation_does_not_wait_for_tool_post_response(self):
        service = TestMcpHttpServer("sse")
        service.stall_sse_tool_post = True
        url = await service.start()
        try:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "sse", "url": url,
                                         "headers": {"Authorization": "Bearer fixture-secret"}}})
            async with KiroSDKClient(options) as client:
                reader, writer = await self._connect(client._mcp_servers[0])
                try:
                    await self._request(reader, writer, 1, "initialize", {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}})
                    await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                    await self._send(writer, {"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                                               "params": {"name": "slow", "arguments": {}}})
                    await asyncio.wait_for(service.started.wait(), 8)
                    # Queue another ordinary POST while the tool POST is held.
                    # Its writer lock must not prevent out-of-band cancellation.
                    await self._send(writer, {"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}})
                    await asyncio.sleep(.05)
                    await asyncio.wait_for(client.request_mcp_tool_cancellation("probe"), 2)
                    self.assertEqual(service.cancellations, [41])
                    result = json.loads(await asyncio.wait_for(reader.readline(), 8))
                    self.assertEqual((result["id"], result["error"]["code"]), (41, -32800))
                    self.assertFalse(service.bad_auth)
                finally:
                    service.release.set()
                    writer.close()
                    await writer.wait_closed()
        finally:
            await service.close()

    async def test_sse_cancellation_uses_each_connection_endpoint(self):
        service = TestMcpHttpServer("sse")
        service.stall_sse_tool_post = True
        service.cancel_release_after = 2
        url = await service.start()
        try:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "sse", "url": url,
                                         "headers": {"Authorization": "Bearer fixture-secret"}}})
            async with KiroSDKClient(options) as client:
                connections = []
                try:
                    for ident in (41, 42):
                        reader, writer = await self._connect(client._mcp_servers[0])
                        connections.append((reader, writer))
                        await self._request(reader, writer, 1, "initialize", {
                            "protocolVersion": "2025-03-26", "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"}})
                        await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                        await self._send(writer, {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                                                   "params": {"name": "slow", "arguments": {}}})
                    for _ in range(200):
                        if len(service.tool_sessions) == 2:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(len(service.tool_sessions), 2)
                    await asyncio.wait_for(client.request_mcp_tool_cancellation("probe"), 2)
                    self.assertEqual(set(service.cancellations), {41, 42})
                    self.assertEqual(set(service.cancellation_sessions), set(service.tool_sessions))
                    self.assertEqual(set(service.cancellation_pairs), set(service.tool_pairs))
                    self.assertEqual(len(set(service.cancellation_sessions)), 2)
                    self.assertFalse(service.bad_auth)
                finally:
                    service.release.set()
                    for _, writer in connections:
                        writer.close()
                        await writer.wait_closed()
        finally:
            await service.close()

    async def test_sse_cancellation_timeout_invalidates_client(self):
        service = TestMcpHttpServer("sse")
        service.stall_sse_tool_post = True
        service.stall_sse_cancel_post = True
        url = await service.start()
        try:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "sse", "url": url,
                                         "headers": {"Authorization": "Bearer fixture-secret"}}})
            async with KiroSDKClient(options) as client:
                reader, writer = await self._connect(client._mcp_servers[0])
                try:
                    await self._request(reader, writer, 1, "initialize", {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}})
                    await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                    await self._send(writer, {"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                                               "params": {"name": "slow", "arguments": {}}})
                    await asyncio.wait_for(service.started.wait(), 8)
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(client.request_mcp_tool_cancellation("probe"), 8)
                    self.assertEqual(service.cancellations, [41])
                    self.assertIsNotNone(client._reader_error)
                    self.assertFalse(service.bad_auth)
                finally:
                    service.release.set()
                    writer.close()
                    await writer.wait_closed()
        finally:
            await service.close()

    async def test_stalled_session_termination_is_bounded(self):
        service = TestMcpHttpServer("http")
        service.stall_delete = True
        url = await service.start()
        try:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": url,
                                         "headers": {"Authorization": "Bearer fixture-secret"}}})
            client = KiroSDKClient(options)
            await client.connect()
            reader, writer = await self._connect(client._mcp_servers[0])
            try:
                await self._request(reader, writer, 1, "initialize", {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"}})
                await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                started = asyncio.get_running_loop().time()
                await asyncio.wait_for(client.disconnect(), 8)
                self.assertLess(asyncio.get_running_loop().time() - started, 7)
                self.assertEqual(service.deletes, 1)
            finally:
                service.release.set()
                writer.close()
                await writer.wait_closed()
                await client.disconnect()
        finally:
            await service.close()

    async def test_modern_http_cancellation_unblocks_client_without_reusing_id(self):
        try:
            from mcp_types.version import MODERN_PROTOCOL_VERSIONS
        except ImportError:
            self.skipTest("modern HTTP cancellation requires MCP 2.x")
        if "2026-07-28" not in MODERN_PROTOCOL_VERSIONS:
            self.skipTest("modern MCP protocol is unavailable")
        service = TestMcpHttpServer("http", protocol_version="2026-07-28")
        service.block_next = True
        url = await service.start()
        try:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": url,
                                         "headers": {"Authorization": "Bearer fixture-secret"}}})
            async with KiroSDKClient(options) as client:
                reader, writer = await self._connect(client._mcp_servers[0])
                try:
                    init = await self._request(reader, writer, 1, "initialize", {
                        "protocolVersion": "2026-07-28", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}})
                    self.assertEqual(init["result"]["protocolVersion"], "2026-07-28")
                    await self._send(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                    await self._send(writer, {"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                                               "params": {"name": "probe", "arguments": {}}})
                    await asyncio.wait_for(service.started.wait(), 8)
                    await client.request_mcp_tool_cancellation("probe")
                    result = json.loads(await asyncio.wait_for(reader.readline(), 8))
                    self.assertEqual((result["id"], result["error"]["code"]), (41, -32800))
                    self.assertEqual(service.cancellations, [])
                    reused = await self._request(reader, writer, 41, "tools/list")
                    self.assertEqual(reused["error"]["code"], -32600)
                    service.release.set()
                    fresh = await self._request(reader, writer, 42, "tools/call", {"name": "probe", "arguments": {}})
                    self.assertEqual(fresh["result"]["content"][0]["text"], "remote-result")
                finally:
                    service.release.set()
                    writer.close()
                    await writer.wait_closed()
        finally:
            await service.close()

    async def test_server_configuration_fails_closed(self):
        good = {"type": "http", "url": "https://example.org/mcp", "headers": {}}
        for value in ("http://user:password@example.org/mcp", "https://example.org/mcp#fragment",
                      "https://example.org/\nsecret", "https://example.org:bad/mcp"):
            with self.subTest(url=value), self.assertRaises(ValueError):
                RemoteMcpBridge({**good, "url": value})
        for headers in ({"Authorization": "x\r\nInjected: bad"}, {"Host": "other"},
                        {"Authorization": "x", "authorization": "y"}, {"Mcp-Session-Id": "x"},
                        {"Accept-Encoding": "gzip"}, {"Authorization": "한글"}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                RemoteMcpBridge({**good, "headers": headers})
        plain = {**good, "headers": {"Authorization": "Bearer synthetic"}}
        for value in ("http://example.org/mcp", "http://10.0.0.5:8080/mcp", "http://localhost.example.org/mcp"):
            with self.subTest(url=value), self.assertRaisesRegex(ValueError, "HTTPS"):
                RemoteMcpBridge({**plain, "url": value})
        for value in ("http://127.0.0.1:8080/mcp", "http://localhost/mcp", "http://[::1]/mcp"):
            with self.subTest(url=value):
                RemoteMcpBridge({**plain, "url": value})
        RemoteMcpBridge({**good, "url": "http://example.org/mcp"})
        with patch("kiro_agent_sdk.remote_mcp.metadata.version", return_value="1.29.0"):
            with self.assertRaisesRegex(RuntimeError, "mcp >=1.30"):
                RemoteMcpBridge(good)
        with patch("kiro_agent_sdk.remote_mcp.metadata.version", return_value="2.2.9"):
            with self.assertRaisesRegex(RuntimeError, "mcp >=1.30"):
                RemoteMcpBridge(good)

    async def test_oversized_and_mislabeled_gzip_remote_responses_are_rejected(self):
        for kind, compression in (("http", False), ("http", True), ("sse", False)):
            with self.subTest(kind=kind, compression=compression):
                service = TestMcpHttpServer(kind)
                url = await service.start()
                try:
                    options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-all",
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
                                service.oversized, service.compressed = not compression, compression
                                await self._send(writer, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                                line = await asyncio.wait_for(reader.readline(), 8)
                                if line:
                                    self.assertIn("error", json.loads(line))
                            finally:
                                writer.close()
                                await writer.wait_closed()
                finally:
                    await service.close()

    async def test_remote_mode_requires_optional_mcp(self):
        with patch("kiro_agent_sdk.remote_mcp.metadata.version", side_effect=PackageNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "optional mcp package"):
                RemoteMcpBridge({"type": "sse", "url": "https://example.org/mcp"})


class ResponseLimiterTests(unittest.TestCase):
    def test_body_and_sse_event_limits_across_chunk_boundaries(self):
        with patch("kiro_agent_sdk.remote_mcp._MAX_REMOTE_MESSAGE", 12):
            body = _ResponseLimiter(sse=False)
            body.feed(b"123456")
            body.feed(b"123456")
            with self.assertRaises(ValueError):
                body.feed(b"7")
            events = _ResponseLimiter(sse=True)
            for chunk in (b"data:a\r", b"\n\r", b"\ndata:b\r\n", b"\r\n"):
                events.feed(chunk)
            oversized = _ResponseLimiter(sse=True)
            with self.assertRaises(ValueError):
                oversized.feed(b"data:long payload\n\n")
