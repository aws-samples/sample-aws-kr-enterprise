import asyncio
import base64
import hashlib
import json
import time
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from kiro_agent_sdk import (CLIConnectionError, InMemoryMcpOAuthStorage, KiroAgentOptions,
                            KiroSDKClient, McpOAuthConfig, ProtocolError, preauthorize_mcp_oauth)
from kiro_agent_sdk.remote_mcp import RemoteMcpBridge

try:
    from mcp import ClientSession
except ImportError:
    ClientSession = None


class OAuthFixture:
    def __init__(self, kind="http"):
        self.kind = kind
        self.server = None
        self.tasks = set()
        self.writer = None
        self.challenge = None
        self.token_grants = []
        self.token_status = 200
        self.token_error_data = {}
        self.refresh_status = 200
        self.refresh_error_data = None
        self.refresh_location = None
        self.registration_status = 201
        self.registration_error_data = {}
        self.registration_redirect = None
        self.authorization_origin = None
        self.discovered_origin = None
        self.redirect_seen = []
        self.requests = []
        self.calls = []
        self.revocations = []
        self.revocation_origin = None
        self.revocation_status = 200
        self.revocation_statuses = []
        self.revocation_redirect = None
        self.initialization_received = None
        self.initialization_release = None
        self.tool_call_received = None
        self.tool_call_release = None
        self.cancellations = []
        self.cancel_redirect = None
        self.cancel_expected_token = None
        self.cancel_reject_status = None
        self.require_auth = True
        self.tool_name = "probe"
        self.tool_result_text = "OAUTH_PROBE_OK"

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.root = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self.root + ("/mcp" if self.kind == "http" else "/sse")

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def authorize(self, url):
        self.redirect_seen.append(url)
        query = parse_qs(urlsplit(url).query)
        assert query["code_challenge_method"] == ["S256"]
        self.challenge = query["code_challenge"][0]
        return "http://127.0.0.1:9999/callback?" + urlencode({"code": "one-time-code", "state": query["state"][0]})

    async def respond(self, writer, status, data, headers=None):
        reasons = {200: "OK", 201: "Created", 202: "Accepted", 204: "No Content", 302: "Found",
                   307: "Temporary Redirect",
                   400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 405: "Method Not Allowed"}
        body = json.dumps(data).encode() if data is not None else b""
        head = f"HTTP/1.1 {status} {reasons[status]}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
        if data is not None:
            head += "Content-Type: application/json\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        writer.write(head.encode() + b"\r\n" + body)
        await writer.drain()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            path = urlsplit(target).path
            headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:] if ": " in line)}
            self.requests.append((path, headers))
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            if path.startswith("/.well-known/oauth-protected-resource"):
                await self.respond(writer, 200, {"resource": self.root + ("/mcp" if self.kind == "http" else "/sse"),
                                                "authorization_servers": [self.discovered_origin or self.root]})
                return
            if path.startswith("/.well-known/oauth-authorization-server"):
                await self.respond(writer, 200, {"issuer": self.root, "authorization_endpoint": (self.authorization_origin or self.root) + "/authorize",
                    "token_endpoint": self.root + "/token", "registration_endpoint": self.root + "/register",
                    "revocation_endpoint": (self.revocation_origin or self.root) + "/revoke",
                    "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"]})
                return
            if path in ("/register", "/register2"):
                registration = json.loads(body)
                if path == "/register" and self.registration_redirect:
                    await self.respond(writer, 307, None, {"Location": self.registration_redirect})
                    return
                if self.registration_status != 201:
                    await self.respond(writer, self.registration_status, self.registration_error_data)
                    return
                await self.respond(writer, 201, {**registration, "client_id": "test-client"})
                return
            if path == "/token":
                fields = parse_qs(body.decode())
                self.token_grants.append(fields["grant_type"][0])
                if fields["grant_type"] == ["authorization_code"]:
                    digest = hashlib.sha256(fields["code_verifier"][0].encode()).digest()
                    assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == self.challenge
                    assert fields["code"] == ["one-time-code"]
                else:
                    assert fields["refresh_token"] == ["refresh-secret"]
                    if self.refresh_status != 200 or self.refresh_error_data is not None:
                        headers = {"Location": self.refresh_location} if self.refresh_location else None
                        await self.respond(writer, self.refresh_status, self.refresh_error_data, headers)
                        return
                if self.token_status != 200:
                    await self.respond(writer, self.token_status, self.token_error_data)
                    return
                await self.respond(writer, 200, {"access_token": "access-secret", "token_type": "Bearer",
                                                "refresh_token": "refresh-secret", "expires_in": 3600})
                return
            if path == "/revoke":
                self.revocations.append(parse_qs(body.decode()))
                status = self.revocation_statuses.pop(0) if self.revocation_statuses else self.revocation_status
                headers = {"Location": self.revocation_redirect} if self.revocation_redirect else None
                await self.respond(writer, status, {}, headers)
                return
            try:
                parsed = json.loads(body) if body else None
            except (UnicodeError, json.JSONDecodeError):
                parsed = None
            cancel = isinstance(parsed, dict) and parsed.get("method") == "notifications/cancelled"
            expected = self.cancel_expected_token if cancel and self.cancel_expected_token is not None else "access-secret"
            if self.require_auth and headers.get("authorization") != "Bearer " + expected:
                await self.respond(writer, 401, None, {"WWW-Authenticate": f'Bearer resource_metadata="{self.root}/.well-known/oauth-protected-resource{path}"'})
                return
            if method == "GET" and path == "/sse":
                self.writer = writer
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: keep-alive\r\n\r\nevent: endpoint\ndata: /messages\n\n")
                await writer.drain()
                await reader.read()
                return
            if method == "GET":
                await self.respond(writer, 405, None)
                return
            if method == "DELETE":
                await self.respond(writer, 204, None)
                return
            msg = json.loads(body)
            if (self.kind == "sse" and msg.get("method") == "notifications/initialized" and
                    self.initialization_received is not None):
                self.initialization_received.set()
                await self.initialization_release.wait()
            if (msg.get("method") == "tools/call" and
                    self.tool_call_received is not None):
                self.tool_call_received.set()
                await self.tool_call_release.wait()
            if msg.get("method") == "notifications/cancelled":
                if self.cancel_reject_status:
                    await self.respond(writer, self.cancel_reject_status, None)
                    return
                if self.cancel_redirect:
                    redirect = self.cancel_redirect
                    self.cancel_redirect = None
                    await self.respond(writer, 307, None, {"Location": redirect})
                    return
                self.cancellations.append(msg.get("params", {}).get("requestId"))
                if self.tool_call_release is not None:
                    self.tool_call_release.set()
            if msg.get("method") == "initialize":
                result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                          "serverInfo": {"name": "oauth-fixture", "version": "1"}}
            elif msg.get("method") == "tools/list":
                result = {"tools": [{"name": self.tool_name, "inputSchema": {"type": "object"}}]}
            elif msg.get("method") == "tools/call":
                self.calls.append(msg.get("params", {}).get("name"))
                result = {"content": [{"type": "text", "text": self.tool_result_text}]}
            else:
                result = {}
            response = {"jsonrpc": "2.0", "id": msg["id"], "result": result} if "id" in msg else None
            if self.kind == "sse":
                if response:
                    self.writer.write(("event: message\ndata: " + json.dumps(response) + "\n\n").encode())
                    await self.writer.drain()
                await self.respond(writer, 202, None)
            elif response:
                await self.respond(writer, 200, response, {"Mcp-Session-Id": "fixture"})
            else:
                await self.respond(writer, 202, None)
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.tasks.discard(task)


@unittest.skipIf(ClientSession is None, "optional MCP dependency is unavailable")
class OAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_shutdown_finishes_initialization_notification(self):
        fixture = OAuthFixture("sse")
        fixture.initialization_received = asyncio.Event()
        fixture.initialization_release = asyncio.Event()
        url = await fixture.start()
        bridge = RemoteMcpBridge({"type": "sse", "url": url,
                                  "headers": {"Authorization": "Bearer access-secret"}})

        async def initialize_and_close():
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

        task = asyncio.create_task(initialize_and_close())
        try:
            await asyncio.wait_for(fixture.initialization_received.wait(), 5)
            self.assertFalse(task.done())
            fixture.initialization_release.set()
            await asyncio.wait_for(task, 5)
        finally:
            fixture.initialization_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await fixture.close()

    async def test_sse_shutdown_does_not_wait_indefinitely_for_notification(self):
        fixture = OAuthFixture("sse")
        fixture.initialization_received = asyncio.Event()
        fixture.initialization_release = asyncio.Event()
        url = await fixture.start()
        bridge = RemoteMcpBridge({"type": "sse", "url": url,
                                  "headers": {"Authorization": "Bearer access-secret"}})

        async def initialize_and_close():
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

        with patch("kiro_agent_sdk.remote_mcp._SSE_INITIALIZATION_SHUTDOWN_TIMEOUT", 0.1):
            task = asyncio.create_task(initialize_and_close())
            try:
                await asyncio.wait_for(fixture.initialization_received.wait(), 5)
                await asyncio.wait_for(task, 2)
            finally:
                fixture.initialization_release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await fixture.close()

    async def test_sse_shutdown_propagates_outer_cancellation(self):
        fixture = OAuthFixture("sse")
        fixture.initialization_received = asyncio.Event()
        fixture.initialization_release = asyncio.Event()
        url = await fixture.start()
        bridge = RemoteMcpBridge({"type": "sse", "url": url,
                                  "headers": {"Authorization": "Bearer access-secret"}})

        async def initialize_and_close():
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

        with patch("kiro_agent_sdk.remote_mcp._SSE_INITIALIZATION_SHUTDOWN_TIMEOUT", 0.1):
            task = asyncio.create_task(initialize_and_close())
            try:
                await asyncio.wait_for(fixture.initialization_received.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            finally:
                fixture.initialization_release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await fixture.close()

    async def test_sse_shutdown_does_not_wait_for_a_tool_call(self):
        fixture = OAuthFixture("sse")
        fixture.tool_call_received = asyncio.Event()
        fixture.tool_call_release = asyncio.Event()
        url = await fixture.start()
        bridge = RemoteMcpBridge({"type": "sse", "url": url,
                                  "headers": {"Authorization": "Bearer access-secret"}})

        async def call_and_close():
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    call = asyncio.create_task(session.call_tool("probe", {}))
                    try:
                        await asyncio.wait_for(fixture.tool_call_received.wait(), 5)
                    finally:
                        call.cancel()
            await asyncio.gather(call, return_exceptions=True)

        try:
            await asyncio.wait_for(call_and_close(), 2)
        finally:
            fixture.tool_call_release.set()
            await fixture.close()

    async def test_sse_oauth_cancellation_during_tool_post(self):
        fixture = OAuthFixture("sse")
        fixture.tool_call_received = asyncio.Event()
        fixture.tool_call_release = asyncio.Event()
        url = await fixture.start()
        fixture.cancel_redirect = fixture.root + "/cancel-next"
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        options = KiroAgentOptions(
            cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
            mcp_servers={"probe": {"type": "sse", "url": url}}, mcp_oauth={"probe": config},
            sdk_mcp_toggle_mode="sdk-all",
        )
        try:
            async with KiroSDKClient(options) as client:
                env = {item["name"]: item["value"] for item in client._mcp_servers[0]["env"]}
                reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
                writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
                await writer.drain()

                async def send(message):
                    writer.write((json.dumps(message) + "\n").encode())
                    await writer.drain()

                try:
                    await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}}})
                    initialized = json.loads(await asyncio.wait_for(reader.readline(), 8))
                    self.assertIn("result", initialized)
                    await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    await send({"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                                "params": {"name": "probe", "arguments": {}}})
                    await asyncio.wait_for(fixture.tool_call_received.wait(), 8)
                    await asyncio.wait_for(client.request_mcp_tool_cancellation("probe"), 2)
                    self.assertEqual(fixture.cancellations, [41])
                    response = json.loads(await asyncio.wait_for(reader.readline(), 8))
                    self.assertEqual(response["id"], 41)
                    self.assertEqual(response["error"]["code"], -32800)
                    authorized = [headers for path, headers in fixture.requests
                                  if path in ("/messages", "/cancel-next")]
                    self.assertTrue(authorized)
                    self.assertTrue(all(h.get("authorization") == "Bearer access-secret" for h in authorized))
                    self.assertTrue(any(path == "/cancel-next" for path, _ in fixture.requests))
                finally:
                    fixture.tool_call_release.set()
                    writer.close()
                    await writer.wait_closed()
        finally:
            fixture.tool_call_release.set()
            await fixture.close()

    async def test_sse_oauth_cancellation_rejects_cross_origin_redirect(self):
        reached = []

        async def target(reader, writer):
            reached.append(await reader.read(1024))
            writer.close()
            await writer.wait_closed()

        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        fixture = OAuthFixture("sse")
        fixture.tool_call_received = asyncio.Event()
        fixture.tool_call_release = asyncio.Event()
        fixture.cancel_redirect = f"http://127.0.0.1:{target_server.sockets[0].getsockname()[1]}/capture"
        url = await fixture.start()
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        options = KiroAgentOptions(
            cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
            mcp_servers={"probe": {"type": "sse", "url": url}}, mcp_oauth={"probe": config},
            sdk_mcp_toggle_mode="sdk-all",
        )
        try:
            async with KiroSDKClient(options) as client:
                env = {item["name"]: item["value"] for item in client._mcp_servers[0]["env"]}
                reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
                writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
                await writer.drain()

                async def send(message):
                    writer.write((json.dumps(message) + "\n").encode())
                    await writer.drain()

                try:
                    await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}}})
                    self.assertIn("result", json.loads(await asyncio.wait_for(reader.readline(), 8)))
                    await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    await send({"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                                "params": {"name": "probe", "arguments": {}}})
                    await asyncio.wait_for(fixture.tool_call_received.wait(), 8)
                    with self.assertRaises(CLIConnectionError) as caught:
                        await client.request_mcp_tool_cancellation("probe")
                    self.assertNotIn("access-secret", str(caught.exception))
                    self.assertNotIn("access-secret", "".join(traceback.format_exception(
                        type(caught.exception), caught.exception, caught.exception.__traceback__)))
                    self.assertEqual(reached, [])
                finally:
                    fixture.tool_call_release.set()
                    writer.close()
                    await writer.wait_closed()
        finally:
            fixture.tool_call_release.set()
            await fixture.close()
            target_server.close()
            await target_server.wait_closed()

    async def test_sse_oauth_cancellation_when_server_allows_anonymous_access(self):
        fixture = OAuthFixture("sse")
        fixture.require_auth = False
        fixture.tool_call_received = asyncio.Event()
        fixture.tool_call_release = asyncio.Event()
        url = await fixture.start()
        bridge = RemoteMcpBridge(
            {"type": "sse", "url": url},
            oauth=McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize),
        )
        env = {item["name"]: item["value"] for item in (await bridge.start())["env"]}
        reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
        await writer.drain()

        async def send(message):
            writer.write((json.dumps(message) + "\n").encode())
            await writer.drain()

        try:
            await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"}}})
            self.assertIn("result", json.loads(await asyncio.wait_for(reader.readline(), 8)))
            await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await send({"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                        "params": {"name": "probe", "arguments": {}}})
            await asyncio.wait_for(fixture.tool_call_received.wait(), 8)
            await asyncio.wait_for(bridge.request_tool_cancellation(), 2)
            self.assertEqual(fixture.cancellations, [41])
            self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 8))["id"], 41)
            self.assertEqual(fixture.token_grants, [])
            self.assertEqual(fixture.redirect_seen, [])
        finally:
            fixture.tool_call_release.set()
            writer.close()
            await writer.wait_closed()
            await bridge.close()
            await fixture.close()

    async def test_revoke_without_tokens_makes_no_network_request(self):
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", lambda url: None)
        bridge = RemoteMcpBridge({"type": "http", "url": "http://127.0.0.1:1/mcp"}, oauth=config)
        self.assertEqual(await bridge._revoke_oauth_tokens(),
                         {"access_token": False, "refresh_token": False})

    async def test_revoke_requests_both_tokens_and_clears_storage(self):
        for kind in ("http", "sse"):
            with self.subTest(kind=kind):
                fixture = OAuthFixture(kind)
                url = await fixture.start()
                config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
                bridge = RemoteMcpBridge({"type": kind, "url": url}, oauth=config)
                try:
                    async with bridge._remote_streams() as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                    result = await bridge._revoke_oauth_tokens()
                    self.assertEqual(result, {"access_token": True, "refresh_token": True})
                    self.assertEqual(fixture.revocations, [
                        {"token": ["refresh-secret"], "token_type_hint": ["refresh_token"],
                         "client_id": ["test-client"]},
                        {"token": ["access-secret"], "token_type_hint": ["access_token"],
                         "client_id": ["test-client"]},
                    ])
                    self.assertIsNone(await config.storage.get_tokens())
                    self.assertIsNone(bridge.oauth.context.current_tokens)
                    self.assertIsNotNone(await config.storage.get_client_info())
                finally:
                    await fixture.close()

    async def test_revoke_rejects_untrusted_origin_without_sending_tokens(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        fixture.revocation_origin = "http://127.0.0.1:1"
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
        try:
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
            with self.assertRaisesRegex(ValueError, "origin is not allowed"):
                await bridge._revoke_oauth_tokens()
            self.assertEqual(fixture.revocations, [])
            self.assertIsNotNone(await config.storage.get_tokens())
        finally:
            await fixture.close()

    async def test_revoke_requires_endpoint_and_public_client_authentication(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
        try:
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
            metadata = bridge.oauth.context.oauth_metadata
            endpoint = metadata.revocation_endpoint
            metadata.revocation_endpoint = None
            with self.assertRaisesRegex(ValueError, "does not advertise"):
                await bridge._revoke_oauth_tokens()
            metadata.revocation_endpoint = endpoint
            metadata.revocation_endpoint_auth_methods_supported = ["client_secret_basic"]
            with self.assertRaisesRegex(ValueError, "unsupported client authentication"):
                await bridge._revoke_oauth_tokens()
            self.assertEqual(fixture.revocations, [])
            self.assertIsNotNone(await config.storage.get_tokens())
        finally:
            await fixture.close()

    async def test_revoke_uses_explicitly_allowed_authorization_origin(self):
        resource, issuer = OAuthFixture(), OAuthFixture()
        url = await resource.start()
        await issuer.start()
        resource.discovered_origin = issuer.root
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", issuer.authorize,
                                allowed_origins=[issuer.root])
        bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
        try:
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
            self.assertEqual(await bridge._revoke_oauth_tokens(),
                             {"access_token": True, "refresh_token": True})
            self.assertEqual(len(issuer.revocations), 2)
            self.assertEqual(resource.revocations, [])
        finally:
            await resource.close()
            await issuer.close()

    async def test_revoke_failure_keeps_tokens_and_hides_http_details(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        fixture.revocation_status = 400
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
        try:
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
            with self.assertRaisesRegex(RuntimeError, "revocation failed") as caught:
                await bridge._revoke_oauth_tokens()
            self.assertNotIn("secret", str(caught.exception))
            self.assertIsNotNone(await config.storage.get_tokens())
            self.assertEqual(len(fixture.revocations), 1)
        finally:
            await fixture.close()

    async def test_revoke_does_not_follow_redirect_with_credentials(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        reached = []
        async def target(reader, writer):
            reached.append(await reader.read(1024))
            writer.close()
            await writer.wait_closed()
        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        fixture.revocation_status = 302
        fixture.revocation_redirect = f"http://127.0.0.1:{target_server.sockets[0].getsockname()[1]}/capture"
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
        try:
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
            with self.assertRaisesRegex(RuntimeError, "revocation failed"):
                await bridge._revoke_oauth_tokens()
            self.assertEqual(reached, [])
            self.assertIsNotNone(await config.storage.get_tokens())
        finally:
            target_server.close()
            await target_server.wait_closed()
            await fixture.close()

    async def test_client_revocation_disconnects_and_clears_credentials(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        options = KiroAgentOptions(
            cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
            mcp_servers={"probe": {"type": "http", "url": url}}, mcp_oauth={"probe": config},
            sdk_mcp_toggle_mode="sdk-all",
        )
        try:
            async with KiroSDKClient(options) as client:
                bridge = client._named_external_mcp_bridges["probe"][0]
                async with bridge._remote_streams() as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                self.assertEqual(await client.revoke_mcp_oauth_tokens("probe"),
                                 {"access_token": True, "refresh_token": True})
                self.assertFalse(client._connected)
                self.assertIsNone(await config.storage.get_tokens())
                self.assertEqual(len(fixture.revocations), 2)
                with self.assertRaises(CLIConnectionError):
                    await client.revoke_mcp_oauth_tokens("probe")
        finally:
            await fixture.close()

    async def test_client_revocation_failure_disconnects_without_exposing_tokens(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        fixture.revocation_statuses = [200, 400]
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        options = KiroAgentOptions(
            cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
            mcp_servers={"probe": {"type": "http", "url": url}}, mcp_oauth={"probe": config},
            sdk_mcp_toggle_mode="sdk-all",
        )
        try:
            async with KiroSDKClient(options) as client:
                bridge = client._named_external_mcp_bridges["probe"][0]
                async with bridge._remote_streams() as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                with self.assertRaisesRegex(CLIConnectionError, "revocation failed") as caught:
                    await client.revoke_mcp_oauth_tokens("probe")
                self.assertNotIn("secret", str(caught.exception))
                self.assertFalse(client._connected)
                self.assertIsNotNone(await config.storage.get_tokens())
                self.assertEqual(len(fixture.revocations), 2)
        finally:
            await fixture.close()

    async def test_client_revocation_hides_storage_errors(self):
        class BrokenStore(InMemoryMcpOAuthStorage):
            failing = False
            async def get_tokens(self):
                if self.failing:
                    raise RuntimeError("private-storage-secret")
                return await super().get_tokens()
        fixture = OAuthFixture()
        url = await fixture.start()
        store = BrokenStore()
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize, storage=store)
        options = KiroAgentOptions(
            cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
            mcp_servers={"probe": {"type": "http", "url": url}}, mcp_oauth={"probe": config},
            sdk_mcp_toggle_mode="sdk-all",
        )
        try:
            async with KiroSDKClient(options) as client:
                bridge = client._named_external_mcp_bridges["probe"][0]
                async with bridge._remote_streams() as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                store.failing = True
                with self.assertRaises(CLIConnectionError) as caught:
                    await client.revoke_mcp_oauth_tokens("probe")
                self.assertNotIn("private-storage-secret", str(caught.exception))
                self.assertFalse(client._connected)
        finally:
            await fixture.close()

    async def test_client_revocation_rejects_an_active_turn(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        options = KiroAgentOptions(
            cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
            mcp_servers={"probe": {"type": "http", "url": url}}, mcp_oauth={"probe": config},
            sdk_mcp_toggle_mode="sdk-all",
        )
        try:
            async with KiroSDKClient(options) as client:
                await client.query("cancel")
                with self.assertRaisesRegex(CLIConnectionError, "current response"):
                    await client.revoke_mcp_oauth_tokens("probe")
                self.assertTrue(client._connected)
                self.assertEqual(fixture.revocations, [])
                await client.interrupt()
                async for _ in client.receive_response():
                    pass
        finally:
            await fixture.close()

    async def test_http_and_sse_pkce_and_refresh(self):
        for kind in ("http", "sse"):
            with self.subTest(kind=kind):
                fixture = OAuthFixture(kind)
                url = await fixture.start()
                config = McpOAuthConfig(redirect_uri="http://127.0.0.1:9999/callback", authorize=fixture.authorize)
                bridge = RemoteMcpBridge({"type": kind, "url": url}, oauth=config)
                try:
                    async with bridge._remote_streams() as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            self.assertEqual([tool.name for tool in (await session.list_tools()).tools], ["probe"])
                            bridge.oauth.context.token_expiry_time = time.time() - 1
                            await session.list_tools()
                    self.assertEqual(fixture.token_grants, ["authorization_code", "refresh_token"])
                    self.assertEqual(len(fixture.redirect_seen), 1)
                finally:
                    await fixture.close()

    async def test_preauthorize_then_reuse_for_http_and_sse(self):
        for kind in ("http", "sse"):
            with self.subTest(kind=kind):
                fixture = OAuthFixture(kind)
                url = await fixture.start()
                config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
                server = {"type": kind, "url": url}
                try:
                    await preauthorize_mcp_oauth(server, config, timeout=10)
                    self.assertIsNotNone(await config.storage.get_tokens())
                    self.assertEqual(fixture.token_grants, ["authorization_code"])
                    bridge = RemoteMcpBridge(server, oauth=config)
                    async with bridge._remote_streams() as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            self.assertEqual([tool.name for tool in (await session.list_tools()).tools], ["probe"])
                    self.assertEqual(fixture.token_grants, ["authorization_code"])
                    self.assertEqual(len(fixture.redirect_seen), 1)
                finally:
                    await fixture.close()

    async def test_preauthorize_timeout_cancels_callback_without_saving_tokens(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def authorize(url):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        config = McpOAuthConfig("http://127.0.0.1:9999/callback", authorize)
        task = asyncio.create_task(preauthorize_mcp_oauth({"type": "http", "url": url}, config, timeout=1))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with self.assertRaises(asyncio.TimeoutError):
                await task
            self.assertTrue(cancelled.is_set())
            self.assertIsNone(await config.storage.get_tokens())
            self.assertEqual(fixture.token_grants, [])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await fixture.close()

    async def test_preauthorize_anonymous_server_does_not_claim_a_grant(self):
        fixture = OAuthFixture()
        fixture.require_auth = False
        url = await fixture.start()
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        try:
            await preauthorize_mcp_oauth({"type": "http", "url": url}, config)
            self.assertIsNone(await config.storage.get_tokens())
            self.assertEqual(fixture.token_grants, [])
            self.assertEqual(fixture.redirect_seen, [])
        finally:
            await fixture.close()

    async def test_preauthorize_rejects_bad_config_and_sanitizes_callback_failure(self):
        fixture = OAuthFixture()
        url = await fixture.start()

        async def authorize(url):
            raise ValueError("sensitive-callback-value")

        config = McpOAuthConfig("http://127.0.0.1:9999/callback", authorize)
        try:
            for timeout in (0, -1, True, float("nan"), float("inf"), "1"):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    await preauthorize_mcp_oauth({"type": "http", "url": url}, config, timeout=timeout)
            with self.assertRaises(ValueError):
                await preauthorize_mcp_oauth({"type": "http", "url": url, "headers": {"X-Test": "x"}}, config)
            with self.assertRaises(CLIConnectionError) as error:
                await preauthorize_mcp_oauth({"type": "http", "url": url}, config, timeout=10)
            self.assertNotIn("sensitive-callback-value", str(error.exception))
            self.assertEqual(fixture.token_grants, [])
        finally:
            await fixture.close()

    async def test_disallowed_authorization_endpoint(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        fixture.authorization_origin = "http://127.0.0.1:1"
        config = McpOAuthConfig(redirect_uri="http://127.0.0.1:9999/callback", authorize=fixture.authorize)
        bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
        try:
            with self.assertRaises(Exception):
                async with bridge._remote_streams() as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
            self.assertEqual(fixture.redirect_seen, [])
            self.assertEqual(fixture.token_grants, [])
        finally:
            await fixture.close()

    async def test_discovery_cannot_contact_unapproved_origin(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        reached = []
        async def target(reader, writer):
            reached.append(await reader.read(1024))
            writer.close()
            await writer.wait_closed()
        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        fixture.discovered_origin = f"http://127.0.0.1:{target_server.sockets[0].getsockname()[1]}"
        config = McpOAuthConfig(redirect_uri="http://127.0.0.1:9999/callback", authorize=fixture.authorize)
        bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
        try:
            with self.assertRaises(Exception):
                async with bridge._remote_streams() as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
            self.assertEqual(reached, [])
            self.assertEqual(fixture.token_grants, [])
        finally:
            target_server.close()
            await target_server.wait_closed()
            await fixture.close()

    async def test_explicit_authorization_server_origin(self):
        resource, issuer = OAuthFixture(), OAuthFixture()
        url = await resource.start()
        await issuer.start()
        resource.discovered_origin = issuer.root
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", issuer.authorize,
                                allowed_origins=[issuer.root])
        bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
        try:
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    self.assertEqual((await session.list_tools()).tools[0].name, "probe")
            self.assertEqual(issuer.token_grants, ["authorization_code"])
            self.assertTrue(any(headers.get("authorization") == "Bearer access-secret"
                                for path, headers in resource.requests if path == "/mcp"))
            self.assertTrue(all(headers.get("authorization") != "Bearer access-secret"
                                for _, headers in issuer.requests))
        finally:
            await resource.close()
            await issuer.close()

    async def test_restart_refuses_storage_without_clear(self):
        class Store:
            async def get_tokens(self): return None
            async def set_tokens(self, value): pass
            async def get_client_info(self): return None
            async def set_client_info(self, value): pass
        fixture = OAuthFixture()
        url = await fixture.start()
        try:
            config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize, storage=Store())
            options = KiroAgentOptions(
                cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
                mcp_servers={"probe": {"type": "http", "url": url}}, mcp_oauth={"probe": config},
                sdk_mcp_toggle_mode="sdk-all")
            async with KiroSDKClient(options) as client:
                with self.assertRaisesRegex(ProtocolError, "clear_tokens"):
                    await client.reconnect_mcp_server("probe", start_oauth=True)
                self.assertTrue((await client.reconnect_mcp_server("probe"))["success"])
        finally:
            await fixture.close()

    async def test_failed_token_clear_does_not_reuse_client_or_expose_error(self):
        class BrokenStore(InMemoryMcpOAuthStorage):
            async def clear_tokens(self):
                raise RuntimeError("secret-from-storage")
        fixture = OAuthFixture()
        url = await fixture.start()
        try:
            config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize,
                                    storage=BrokenStore())
            options = KiroAgentOptions(
                cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
                mcp_servers={"probe": {"type": "http", "url": url}}, mcp_oauth={"probe": config},
                sdk_mcp_toggle_mode="sdk-all")
            async with KiroSDKClient(options) as client:
                with self.assertRaises(CLIConnectionError) as caught:
                    await client.reconnect_mcp_server("probe", start_oauth=True)
                self.assertNotIn("secret-from-storage", str(caught.exception))
                self.assertIsNotNone(client._reader_error)
                with self.assertRaises(CLIConnectionError):
                    await client.reconnect_mcp_server("probe")
        finally:
            await fixture.close()

    async def test_hung_token_clear_aborts_client(self):
        class HungStore(InMemoryMcpOAuthStorage):
            async def clear_tokens(self):
                await asyncio.Event().wait()
        fixture = OAuthFixture()
        url = await fixture.start()
        try:
            config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize, storage=HungStore())
            options = KiroAgentOptions(
                cli_path=str(Path(__file__).with_name("fake_kiro.py")), agent_engine="v3",
                mcp_servers={"probe": {"type": "http", "url": url}}, mcp_oauth={"probe": config},
                sdk_mcp_toggle_mode="sdk-all")
            async with KiroSDKClient(options) as client:
                with self.assertRaises(CLIConnectionError):
                    await client.reconnect_mcp_server("probe", start_oauth=True)
                self.assertIsNotNone(client._reader_error)
                with self.assertRaises(CLIConnectionError):
                    await client.reconnect_mcp_server("probe")
        finally:
            await fixture.close()

    async def test_reject_bad_callback_and_static_headers(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        async def callback(url):
            query = parse_qs(urlsplit(url).query)
            return "http://127.0.0.1:9999/other?" + urlencode({"code": "code", "state": query["state"][0]})
        config = McpOAuthConfig(redirect_uri="http://127.0.0.1:9999/callback", authorize=callback)
        try:
            with self.assertRaisesRegex(ValueError, "static headers"):
                RemoteMcpBridge({"type": "http", "url": url, "headers": {"X-Test": "secret"}}, oauth=config)
            bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
            with self.assertRaises(Exception):
                async with bridge._remote_streams() as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
            self.assertEqual(fixture.token_grants, [])
        finally:
            await fixture.close()

    async def test_state_and_issuer_mismatch_prevent_token_exchange(self):
        for mismatch in ("state", "issuer"):
            with self.subTest(mismatch=mismatch):
                fixture = OAuthFixture()
                url = await fixture.start()
                expected_states = []
                async def callback(auth_url):
                    query = parse_qs(urlsplit(auth_url).query)
                    expected_states.append(query["state"][0])
                    fields = {"code": "one-time-code", "state": query["state"][0]}
                    if mismatch == "state":
                        fields["state"] = "state-private-test"
                    else:
                        fields["iss"] = "https://wrong.example/issuer-private-test"
                    return "http://127.0.0.1:9999/callback?" + urlencode(fields)
                config = McpOAuthConfig("http://127.0.0.1:9999/callback", callback)
                bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
                try:
                    with self.assertLogs("mcp.client.auth.oauth2", level="ERROR") as logs:
                        with self.assertRaises(Exception):
                            async with bridge._remote_streams() as (reader, writer):
                                async with ClientSession(reader, writer) as session:
                                    await session.initialize()
                    self.assertEqual(fixture.token_grants, [])
                    rendered = "\n".join(logs.output)
                    self.assertNotIn("state-private-test", rendered)
                    self.assertNotIn("issuer-private-test", rendered)
                    for state in expected_states:
                        self.assertNotIn(state, rendered)
                finally:
                    await fixture.close()

    async def test_token_exchange_error_body_is_not_logged(self):
        fixture = OAuthFixture()
        fixture.token_status = 400
        fixture.token_error_data = {"error_description": "token-response-private-test"}
        url = await fixture.start()
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        try:
            with self.assertLogs("mcp.client.auth.oauth2", level="ERROR") as logs:
                with self.assertRaises(CLIConnectionError):
                    await preauthorize_mcp_oauth({"type": "http", "url": url}, config, timeout=10)
            self.assertNotIn("token-response-private-test", "\n".join(logs.output))
        finally:
            await fixture.close()

    async def test_registration_error_body_is_not_logged(self):
        for status, body in ((400, {"error_description": "registration-private-test"}),
                             (200, {"client_id": {"private": "registration-private-test"}})):
            with self.subTest(status=status):
                fixture = OAuthFixture()
                fixture.registration_status, fixture.registration_error_data = status, body
                url = await fixture.start()
                config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
                try:
                    with self.assertNoLogs("mcp.client.auth.oauth2", level="WARNING"):
                        with self.assertRaises(CLIConnectionError):
                            await preauthorize_mcp_oauth({"type": "http", "url": url}, config, timeout=10)
                finally:
                    await fixture.close()

    async def test_registration_redirects_are_bounded_and_same_origin(self):
        from mcp.shared._httpx_utils import _AUTH_REDIRECT_LIMIT
        for location, succeeds in (("/register2", True), ("/register", False),
                                    ("https://example.com/registration-private-test", False)):
            with self.subTest(location=location):
                fixture = OAuthFixture()
                fixture.registration_redirect = location
                url = await fixture.start()
                config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
                try:
                    with self.assertNoLogs("mcp.client.auth.oauth2", level="WARNING"):
                        if succeeds:
                            await preauthorize_mcp_oauth({"type": "http", "url": url}, config, timeout=10)
                            self.assertEqual(fixture.token_grants, ["authorization_code"])
                        else:
                            with self.assertRaises(CLIConnectionError):
                                await preauthorize_mcp_oauth({"type": "http", "url": url}, config, timeout=10)
                            self.assertEqual(fixture.token_grants, [])
                    self.assertLessEqual(sum(path == "/register" for path, _ in fixture.requests),
                                         _AUTH_REDIRECT_LIMIT + 1)
                finally:
                    await fixture.close()

    async def test_unsupported_registration_auth_method_is_not_logged(self):
        for method in ("registration-private-test", "private_key_jwt", "client_secret_basic"):
            with self.subTest(method=method):
                fixture = OAuthFixture()
                fixture.registration_status = 200
                fixture.registration_error_data = {
                    "client_id": "fixture-client", "token_endpoint_auth_method": method,
                    "redirect_uris": ["http://127.0.0.1:9999/callback"]}
                url = await fixture.start()
                config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
                try:
                    with self.assertNoLogs("mcp.client.auth.oauth2", level="WARNING"):
                        with self.assertRaises(CLIConnectionError):
                            await preauthorize_mcp_oauth({"type": "http", "url": url}, config, timeout=10)
                finally:
                    await fixture.close()

    async def test_registered_client_secret_auth_methods_remain_supported(self):
        for method in ("client_secret_basic", "client_secret_post"):
            with self.subTest(method=method):
                fixture = OAuthFixture()
                fixture.registration_status = 200
                fixture.registration_error_data = {
                    "client_id": "fixture-client", "client_secret": "fixture-secret",
                    "token_endpoint_auth_method": method,
                    "redirect_uris": ["http://127.0.0.1:9999/callback"]}
                url = await fixture.start()
                config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
                try:
                    await preauthorize_mcp_oauth({"type": "http", "url": url}, config, timeout=10)
                    self.assertEqual(fixture.token_grants, ["authorization_code"])
                    token_headers = next(headers for path, headers in fixture.requests if path == "/token")
                    self.assertEqual(token_headers.get("authorization", "").startswith("Basic "),
                                     method == "client_secret_basic")
                finally:
                    await fixture.close()

    async def test_registration_failure_releases_connection_for_retry(self):
        fixture = OAuthFixture()
        fixture.registration_status = 400
        url = await fixture.start()
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
        try:
            with self.assertNoLogs("mcp.client.auth.oauth2", level="WARNING"):
                with self.assertRaises(Exception):
                    async with bridge._remote_streams() as (reader, writer):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                fixture.registration_status = 201
                async with bridge._remote_streams() as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await asyncio.wait_for(session.initialize(), 10)
            self.assertEqual(fixture.token_grants, ["authorization_code"])
        finally:
            await fixture.close()

    async def test_refresh_failure_does_not_log_response_or_redirect(self):
        for mode in ("invalid-token", "redirect"):
            with self.subTest(mode=mode):
                fixture = OAuthFixture()
                if mode == "invalid-token":
                    fixture.refresh_error_data = {"access_token": {"private": "refresh-private-test"},
                                                  "token_type": "Bearer"}
                else:
                    fixture.refresh_status = 302
                    fixture.refresh_location = "https://example.com/refresh-private-test"
                url = await fixture.start()
                config = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
                bridge = RemoteMcpBridge({"type": "http", "url": url}, oauth=config)
                try:
                    with self.assertNoLogs("mcp.client.auth.oauth2", level="WARNING"):
                        async with bridge._remote_streams() as (reader, writer):
                            async with ClientSession(reader, writer) as session:
                                await session.initialize()
                                bridge.oauth.context.token_expiry_time = time.time() - 1
                                self.assertEqual([tool.name for tool in (await session.list_tools()).tools], ["probe"])
                    self.assertEqual(fixture.token_grants, ["authorization_code", "refresh_token",
                                                            "authorization_code"])
                finally:
                    await fixture.close()

    def test_reject_invalid_configuration(self):
        async def callback(url):
            return url
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", callback)
        with self.assertRaisesRegex(ValueError, "sdk-all"):
            KiroSDKClient(KiroAgentOptions(mcp_oauth={"remote": config}))
        with self.assertRaisesRegex(ValueError, "McpOAuthConfig"):
            KiroSDKClient(KiroAgentOptions(sdk_mcp_toggle_mode="sdk-all",
                                           mcp_oauth={"one": config, "two": config}))
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            RemoteMcpBridge({"type": "http", "url": "http://example.com/mcp"}, oauth=config)
        config.allowed_origins = ["https://example.com/anything"]
        with self.assertRaisesRegex(ValueError, "OAuth URL"):
            RemoteMcpBridge({"type": "http", "url": "https://example.com/mcp"}, oauth=config)
