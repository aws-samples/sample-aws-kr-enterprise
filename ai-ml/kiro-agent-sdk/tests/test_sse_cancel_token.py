import asyncio
import json
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient, McpOAuthConfig
from kiro_agent_sdk.remote_mcp import RemoteMcpBridge
from test_mcp_oauth import ClientSession, OAuthFixture

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


@unittest.skipIf(ClientSession is None, "optional MCP dependency is unavailable")
class SseCancellationTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = OAuthFixture("sse")
        self.fixture.tool_call_received = asyncio.Event()
        self.fixture.tool_call_release = asyncio.Event()
        self.url = await self.fixture.start()
        self.client = None
        self.writer = None

    async def asyncTearDown(self):
        self.fixture.tool_call_release.set()
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        if self.client is not None:
            await self.client.disconnect()
        await self.fixture.close()
        self.tmp.cleanup()

    async def begin(self, provider, *, stored=False, storage=None):
        extra = {} if storage is None else {"storage": storage}
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", self.fixture.authorize,
                                sse_cancel_token_provider=provider,
                                sse_cancel_retry_with_stored_token=stored, **extra)
        options = KiroAgentOptions(cwd=self.tmp.name, cli_path=FAKE, agent_engine="v3",
            env={"KIRO_HOME": str(Path(self.tmp.name) / "kiro")}, sdk_mcp_toggle_mode="sdk-all",
            mcp_servers={"probe": {"type": "sse", "url": self.url}}, mcp_oauth={"probe": config})
        self.client = KiroSDKClient(options)
        await self.client.connect()
        env = {item["name"]: item["value"] for item in self.client._mcp_servers[0]["env"]}
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        self.writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
        await self.writer.drain()

        async def send(message):
            self.writer.write((json.dumps(message) + "\n").encode())
            await self.writer.drain()

        await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}})
        self.assertIn("result", json.loads(await asyncio.wait_for(self.reader.readline(), 8)))
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                    "params": {"name": "probe", "arguments": {}}})
        await asyncio.wait_for(self.fixture.tool_call_received.wait(), 8)
        return config

    def cancellation_headers(self):
        return [headers.get("authorization") for path, headers in self.fixture.requests
                if path == "/messages"][-2:]

    async def test_unauthorized_cancel_retries_once_with_fresh_token(self):
        calls = []
        async def provider():
            calls.append(True)
            return "renewed-secret"
        config = await self.begin(provider)
        self.assertNotIn("provider", repr(config))
        self.fixture.cancel_expected_token = "renewed-secret"
        await asyncio.wait_for(self.client.request_mcp_tool_cancellation("probe"), 3)
        self.assertEqual(self.fixture.cancellations, [41])
        self.assertEqual(self.cancellation_headers(), ["Bearer access-secret", "Bearer renewed-secret"])
        self.assertEqual(calls, [True])
        self.assertEqual(self.fixture.token_grants, ["authorization_code"])
        self.assertEqual(json.loads(await asyncio.wait_for(self.reader.readline(), 8))["id"], 41)

    async def test_provider_is_not_called_when_original_token_succeeds(self):
        calls = []
        async def provider():
            calls.append(True)
            return "renewed-secret"
        await self.begin(provider)
        await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.fixture.cancellations, [41])
        self.assertEqual(calls, [])

    async def test_provider_can_authorize_a_previously_anonymous_connection(self):
        self.fixture.require_auth = False
        async def provider():
            return "renewed-secret"
        await self.begin(provider)
        self.fixture.require_auth = True
        self.fixture.cancel_expected_token = "renewed-secret"
        await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.cancellation_headers(), [None, "Bearer renewed-secret"])
        self.assertEqual(self.fixture.cancellations, [41])

    async def test_fresh_token_can_follow_same_origin_redirect(self):
        calls = []
        async def provider():
            calls.append(True)
            return "renewed-secret"
        await self.begin(provider)
        self.fixture.cancel_expected_token = "renewed-secret"
        self.fixture.cancel_redirect = self.fixture.root + "/cancel-next"
        await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.fixture.cancellations, [41])
        self.assertEqual(calls, [True])
        self.assertIn(("/cancel-next", "Bearer renewed-secret"), [
            (path, headers.get("authorization")) for path, headers in self.fixture.requests])

    async def test_invalid_token_and_callback_exception_are_sanitized(self):
        for value in ("bad\nsecret", "", "x" * 16385, RuntimeError("PRIVATE_PROVIDER_ERROR")):
            with self.subTest(value=type(value).__name__):
                async def provider():
                    if isinstance(value, Exception):
                        raise value
                    return value
                await self.begin(provider)
                self.fixture.cancel_expected_token = "renewed-secret"
                with self.assertRaises(CLIConnectionError) as caught:
                    await self.client.request_mcp_tool_cancellation("probe")
                formatted = "".join(traceback.format_exception(
                    type(caught.exception), caught.exception, caught.exception.__traceback__))
                self.assertNotIn("PRIVATE_PROVIDER_ERROR", formatted)
                self.assertNotIn("bad\nsecret", formatted)
                self.assertEqual(self.fixture.cancellations, [])
                self.fixture.tool_call_release.set()
                self.writer.close()
                await self.writer.wait_closed()
                self.writer = None
                await self.client.disconnect()
                self.client = None
                await self.fixture.close()
                self.fixture = OAuthFixture("sse")
                self.fixture.tool_call_received = asyncio.Event()
                self.fixture.tool_call_release = asyncio.Event()
                self.url = await self.fixture.start()

    async def test_wrong_fresh_token_is_not_retried_again(self):
        calls = []
        async def provider():
            calls.append(True)
            return "wrong-secret"
        await self.begin(provider)
        self.fixture.cancel_expected_token = "renewed-secret"
        with self.assertRaises(CLIConnectionError) as caught:
            await self.client.request_mcp_tool_cancellation("probe")
        cause = caught.exception.__cause__
        self.assertIsNotNone(cause)
        self.assertIsNone(cause.__context__)
        self.assertNotIn("wrong-secret", repr(cause))
        tb = cause.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_name != "send_sse_cancellation":
            tb = tb.tb_next
        self.assertIsNotNone(tb)
        for name in ("response", "token", "task", "done", "_", "cancel_client", "headers"):
            self.assertIsNone(tb.tb_frame.f_locals.get(name))
        self.assertEqual(self.cancellation_headers(), ["Bearer access-secret", "Bearer wrong-secret"])
        self.assertEqual(calls, [True])
        self.assertEqual(self.fixture.cancellations, [])

    async def test_non_401_failure_does_not_invoke_provider(self):
        calls = []
        async def provider():
            calls.append(True)
            return "renewed-secret"
        await self.begin(provider)
        self.fixture.cancel_reject_status = 403
        with self.assertRaises(CLIConnectionError):
            await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(calls, [])
        self.assertEqual(self.fixture.cancellations, [])

    async def test_timeout_discards_uncooperative_callback_without_late_retry(self):
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def provider():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return "renewed-secret"
        await self.begin(provider)
        self.fixture.cancel_expected_token = "renewed-secret"
        bridge = self.client._named_external_mcp_bridges["probe"][0]
        try:
            with patch("kiro_agent_sdk.remote_mcp._SSE_CANCEL_TOKEN_TIMEOUT", 0.05):
                with self.assertRaises(CLIConnectionError):
                    await asyncio.wait_for(self.client.request_mcp_tool_cancellation("probe"), 2)
            self.assertTrue(started.is_set())
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0.05)
            self.assertEqual(bridge._sse_cancel_token_tasks, set())
            self.assertEqual(self.fixture.cancellations, [])
            self.assertNotIn("Bearer renewed-secret",
                             [headers.get("authorization") for _, headers in self.fixture.requests])
        finally:
            release.set()

    async def test_fresh_token_is_never_sent_across_origin(self):
        reached = []
        async def target(reader, writer):
            reached.append(await reader.read(4096))
            writer.close()
            await writer.wait_closed()
        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        try:
            async def provider():
                return "renewed-secret"
            await self.begin(provider)
            self.fixture.cancel_expected_token = "renewed-secret"
            self.fixture.cancel_redirect = f"http://127.0.0.1:{target_server.sockets[0].getsockname()[1]}/capture"
            with self.assertRaises(CLIConnectionError) as caught:
                await self.client.request_mcp_tool_cancellation("probe")
            formatted = "".join(traceback.format_exception(
                type(caught.exception), caught.exception, caught.exception.__traceback__))
            self.assertNotIn("renewed-secret", formatted)
            self.assertEqual(reached, [])
        finally:
            target_server.close()
            await target_server.wait_closed()

    async def test_caller_cancellation_discards_late_token(self):
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def provider():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return "renewed-secret"
        await self.begin(provider)
        self.fixture.cancel_expected_token = "renewed-secret"
        task = asyncio.create_task(self.client.request_mcp_tool_cancellation("probe"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0.05)
            self.assertEqual(self.fixture.cancellations, [])
            self.assertNotIn("Bearer renewed-secret",
                             [headers.get("authorization") for _, headers in self.fixture.requests])
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_provider_change_requires_reconnect(self):
        async def provider():
            return "renewed-secret"
        config = await self.begin(provider)
        config.sse_cancel_token_provider = None
        with self.assertRaises(CLIConnectionError):
            await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.fixture.cancellations, [])

    async def test_provider_change_during_callback_does_not_send_token(self):
        config = None
        async def provider():
            config.sse_cancel_token_provider = None
            return "renewed-secret"
        config = await self.begin(provider)
        self.fixture.cancel_expected_token = "renewed-secret"
        with self.assertRaises(CLIConnectionError):
            await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.fixture.cancellations, [])
        self.assertNotIn("Bearer renewed-secret",
                         [headers.get("authorization") for _, headers in self.fixture.requests])

    async def test_invalid_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "async callback"):
            RemoteMcpBridge({"type": "sse", "url": self.url}, oauth=McpOAuthConfig(
                "http://127.0.0.1:9999/callback", self.fixture.authorize, sse_cancel_token_provider="bad"))
        with self.assertRaisesRegex(ValueError, "SSE"):
            RemoteMcpBridge({"type": "http", "url": self.fixture.root + "/mcp"}, oauth=McpOAuthConfig(
                "http://127.0.0.1:9999/callback", self.fixture.authorize, sse_cancel_token_provider=lambda: None))
