import asyncio
import traceback
import unittest
from unittest.mock import patch

from kiro_agent_sdk import CLIConnectionError, InMemoryMcpOAuthStorage, McpOAuthConfig
from kiro_agent_sdk.remote_mcp import RemoteMcpBridge
import test_sse_cancel_token as helpers

if helpers.ClientSession is not None:
    from mcp.shared.auth import OAuthToken


class ControlledStorage(InMemoryMcpOAuthStorage):
    def __init__(self):
        super().__init__()
        self.reads = 0
        self.override = False
        self.value = None
        self.error = None
        self.started = None
        self.release = None
        self.finished = None
        self.ignore_cancellation = False

    async def get_tokens(self):
        self.reads += 1
        if self.started is not None:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.ignore_cancellation:
                    raise
                await self.release.wait()
            if self.finished is not None:
                self.finished.set()
        if self.error is not None:
            raise self.error
        return self.value if self.override else await super().get_tokens()


@unittest.skipIf(helpers.ClientSession is None, "optional MCP dependency is unavailable")
class SseStoredCancellationTokenTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.SseCancellationTokenTests.asyncSetUp
    asyncTearDown = helpers.SseCancellationTokenTests.asyncTearDown
    begin = helpers.SseCancellationTokenTests.begin

    async def setup_stored(self):
        storage = ControlledStorage()
        config = await self.begin(None, stored=True, storage=storage)
        storage.reads = 0
        return config, storage

    def cancellation_requests_since(self, index):
        return [headers.get("authorization") for path, headers in self.fixture.requests[index:]
                if path == "/messages"]

    async def put_new_token(self, storage):
        await storage.set_tokens(OAuthToken(access_token="renewed-secret", token_type="Bearer",
                                            refresh_token="private-refresh", expires_in=3600))
        self.fixture.cancel_expected_token = "renewed-secret"

    async def test_retries_with_changed_stored_token_without_refreshing(self):
        config, storage = await self.setup_stored()
        self.assertTrue(config.sse_cancel_retry_with_stored_token)
        await self.put_new_token(storage)
        before = len(self.fixture.requests)
        await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.cancellation_requests_since(before), ["Bearer access-secret", "Bearer renewed-secret"])
        self.assertEqual(self.fixture.cancellations, [41])
        self.assertEqual(storage.reads, 1)
        self.assertEqual(self.fixture.token_grants, ["authorization_code"])

    async def test_oauth_token_subclass_from_storage_is_supported(self):
        class AppOAuthToken(OAuthToken):
            pass

        _, storage = await self.setup_stored()
        await storage.set_tokens(AppOAuthToken(access_token="renewed-secret", token_type="Bearer"))
        self.fixture.cancel_expected_token = "renewed-secret"
        await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.fixture.cancellations, [41])

    async def test_storage_is_not_read_when_first_cancellation_succeeds(self):
        _, storage = await self.setup_stored()
        await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(storage.reads, 0)
        self.assertEqual(self.fixture.cancellations, [41])

    async def test_default_mode_does_not_use_updated_storage(self):
        storage = ControlledStorage()
        await self.begin(None, storage=storage)
        storage.reads = 0
        await self.put_new_token(storage)
        before = len(self.fixture.requests)
        with self.assertRaises(CLIConnectionError):
            await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.cancellation_requests_since(before), ["Bearer access-secret"])
        self.assertEqual(storage.reads, 0)

    async def test_unchanged_token_does_not_repeat_the_failed_request(self):
        _, storage = await self.setup_stored()
        self.fixture.cancel_expected_token = "renewed-secret"
        before = len(self.fixture.requests)
        with self.assertRaises(CLIConnectionError) as caught:
            await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.cancellation_requests_since(before), ["Bearer access-secret"])
        self.assertEqual(storage.reads, 1)
        formatted = "".join(traceback.format_exception(
            type(caught.exception), caught.exception, caught.exception.__traceback__))
        self.assertNotIn("access-secret", formatted)
        cause = caught.exception.__cause__
        self.assertIsNotNone(cause)
        self.assertIsNone(cause.__context__)
        tb = cause.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_name != "send_sse_cancellation":
            tb = tb.tb_next
        self.assertIsNotNone(tb)
        for name in ("response", "token", "task", "headers", "previous_authorization", "retry_token"):
            self.assertIsNone(tb.tb_frame.f_locals.get(name))

    async def test_invalid_or_expired_stored_tokens_are_rejected(self):
        for value in (None, {"access_token": "PRIVATE-DICT"},
                      OAuthToken(access_token="bad\nsecret", token_type="Bearer"),
                      OAuthToken(access_token="PRIVATE-EXPIRED", token_type="Bearer", expires_in=0),
                      OAuthToken.model_construct(access_token="PRIVATE-TYPE", token_type="Basic")):
            with self.subTest(kind=type(value).__name__):
                config, storage = await self.setup_stored()
                storage.override, storage.value = True, value
                self.fixture.cancel_expected_token = "renewed-secret"
                before = len(self.fixture.requests)
                with self.assertRaises(CLIConnectionError) as caught:
                    await self.client.request_mcp_tool_cancellation("probe")
                self.assertEqual(self.cancellation_requests_since(before), ["Bearer access-secret"])
                formatted = "".join(traceback.format_exception(
                    type(caught.exception), caught.exception, caught.exception.__traceback__))
                self.assertNotIn("PRIVATE", formatted)
                self.assertNotIn("bad\nsecret", formatted)
                self.fixture.tool_call_release.set()
                self.writer.close()
                await self.writer.wait_closed()
                self.writer = None
                await self.client.disconnect()
                self.client = None
                await self.fixture.close()
                self.fixture = helpers.OAuthFixture("sse")
                self.fixture.tool_call_received = asyncio.Event()
                self.fixture.tool_call_release = asyncio.Event()
                self.url = await self.fixture.start()

    async def test_storage_exception_is_sanitized_and_does_not_retry(self):
        _, storage = await self.setup_stored()
        storage.error = RuntimeError("PRIVATE-STORAGE-ERROR")
        self.fixture.cancel_expected_token = "renewed-secret"
        before = len(self.fixture.requests)
        with self.assertRaises(CLIConnectionError) as caught:
            await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.cancellation_requests_since(before), ["Bearer access-secret"])
        self.assertNotIn("PRIVATE-STORAGE-ERROR", "".join(traceback.format_exception(
            type(caught.exception), caught.exception, caught.exception.__traceback__)))

    async def test_non_401_does_not_read_stored_tokens(self):
        _, storage = await self.setup_stored()
        self.fixture.cancel_reject_status = 403
        with self.assertRaises(CLIConnectionError):
            await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(storage.reads, 0)

    async def test_mode_or_storage_change_during_read_cannot_send_token(self):
        for change in ("mode", "storage", "reader"):
            with self.subTest(change=change):
                config, storage = await self.setup_stored()
                await self.put_new_token(storage)
                storage.started, storage.release = asyncio.Event(), asyncio.Event()
                pending = asyncio.create_task(self.client.request_mcp_tool_cancellation("probe"))
                try:
                    await asyncio.wait_for(storage.started.wait(), 2)
                    if change == "mode":
                        config.sse_cancel_retry_with_stored_token = False
                    elif change == "storage":
                        config.storage = InMemoryMcpOAuthStorage()
                    else:
                        async def other():
                            return OAuthToken(access_token="other-secret", token_type="Bearer")
                        storage.get_tokens = other
                    storage.release.set()
                    with self.assertRaises(CLIConnectionError):
                        await asyncio.wait_for(pending, 2)
                    self.assertEqual(self.fixture.cancellations, [])
                    self.assertNotIn("Bearer renewed-secret",
                                     [headers.get("authorization") for _, headers in self.fixture.requests])
                finally:
                    storage.release.set()
                    self.fixture.tool_call_release.set()
                    self.writer.close()
                    await self.writer.wait_closed()
                    self.writer = None
                    await self.client.disconnect()
                    self.client = None
                    await self.fixture.close()
                    self.fixture = helpers.OAuthFixture("sse")
                    self.fixture.tool_call_received = asyncio.Event()
                    self.fixture.tool_call_release = asyncio.Event()
                    self.url = await self.fixture.start()

    async def test_timeout_does_not_send_late_stored_token(self):
        _, storage = await self.setup_stored()
        await self.put_new_token(storage)
        storage.started, storage.release, storage.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        storage.ignore_cancellation = True
        bridge = self.client._named_external_mcp_bridges["probe"][0]
        try:
            with patch("kiro_agent_sdk.remote_mcp._SSE_CANCEL_TOKEN_TIMEOUT", .05):
                with self.assertRaises(CLIConnectionError):
                    await asyncio.wait_for(self.client.request_mcp_tool_cancellation("probe"), 2)
            self.assertTrue(storage.started.is_set())
            storage.release.set()
            await asyncio.wait_for(storage.finished.wait(), 2)
            await asyncio.sleep(.05)
            self.assertEqual(bridge._sse_cancel_token_tasks, set())
            self.assertEqual(self.fixture.cancellations, [])
            self.assertNotIn("Bearer renewed-secret",
                             [headers.get("authorization") for _, headers in self.fixture.requests])
        finally:
            storage.release.set()

    async def test_caller_cancellation_does_not_send_late_stored_token(self):
        _, storage = await self.setup_stored()
        await self.put_new_token(storage)
        storage.started, storage.release, storage.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        storage.ignore_cancellation = True
        pending = asyncio.create_task(self.client.request_mcp_tool_cancellation("probe"))
        try:
            await asyncio.wait_for(storage.started.wait(), 2)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            storage.release.set()
            await asyncio.wait_for(storage.finished.wait(), 2)
            await asyncio.sleep(.05)
            self.assertEqual(self.fixture.cancellations, [])
            self.assertNotIn("Bearer renewed-secret",
                             [headers.get("authorization") for _, headers in self.fixture.requests])
        finally:
            storage.release.set()

    async def test_stored_token_can_follow_same_origin_redirect(self):
        _, storage = await self.setup_stored()
        await self.put_new_token(storage)
        self.fixture.cancel_redirect = self.fixture.root + "/cancel-next"
        await self.client.request_mcp_tool_cancellation("probe")
        self.assertEqual(self.fixture.cancellations, [41])
        self.assertIn(("/cancel-next", "Bearer renewed-secret"), [
            (path, headers.get("authorization")) for path, headers in self.fixture.requests])

    async def test_stored_token_never_follows_cross_origin_redirect(self):
        reached = []
        async def target(reader, writer):
            reached.append(await reader.read(4096))
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(target, "127.0.0.1", 0)
        try:
            _, storage = await self.setup_stored()
            await self.put_new_token(storage)
            self.fixture.cancel_redirect = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/capture"
            with self.assertRaises(CLIConnectionError) as caught:
                await self.client.request_mcp_tool_cancellation("probe")
            self.assertNotIn("renewed-secret", "".join(traceback.format_exception(
                type(caught.exception), caught.exception, caught.exception.__traceback__)))
            self.assertEqual(reached, [])
        finally:
            server.close()
            await server.wait_closed()

    async def test_invalid_configuration_is_rejected(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RemoteMcpBridge({"type": "sse", "url": self.url}, oauth=McpOAuthConfig(
                    "http://127.0.0.1:9999/callback", self.fixture.authorize,
                    sse_cancel_retry_with_stored_token=value))
        with self.assertRaises(ValueError):
            RemoteMcpBridge({"type": "http", "url": self.url}, oauth=McpOAuthConfig(
                "http://127.0.0.1:9999/callback", self.fixture.authorize,
                sse_cancel_retry_with_stored_token=True))
        with self.assertRaises(ValueError):
            RemoteMcpBridge({"type": "sse", "url": self.url}, oauth=McpOAuthConfig(
                "http://127.0.0.1:9999/callback", self.fixture.authorize,
                sse_cancel_retry_with_stored_token=True, sse_cancel_token_provider=lambda: None))
        config = McpOAuthConfig("http://127.0.0.1:9999/callback", self.fixture.authorize,
                                sse_cancel_retry_with_stored_token=True)
        state = config._create_provider(self.url)
        config.storage = InMemoryMcpOAuthStorage()
        with self.assertRaises(ValueError):
            RemoteMcpBridge({"type": "sse", "url": self.url}, oauth=config, _oauth_state=state)
