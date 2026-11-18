import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from test_agentcore import FAKE, HEADER, HAS_STARLETTE

if HAS_STARLETTE:
    from starlette.requests import Request
    from kiro_agent_sdk import KiroAgentOptions
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreAuthorizerBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.apps = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    def app(self, authorize, *, limit=32, reauthorize=False):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / f"state-{len(self.apps)}", cli_path=str(FAKE),
                                 api_key="synthetic", max_body_bytes=limit, reauthorize_output=reauthorize)
        app = create_agentcore_app(config, options_factory=lambda workspace, resume: KiroAgentOptions(tools=[]),
                                   session_authorizer=authorize)
        self.apps.append(app)
        return app

    async def chunks(self, app, chunks):
        incoming = [{"type": "http.request", "body": chunk, "more_body": i != len(chunks) - 1}
                    for i, chunk in enumerate(chunks)]
        sent, consumed = [], []
        async def receive():
            if incoming:
                item = incoming.pop(0)
                consumed.append(item["body"])
                return item
            await asyncio.Event().wait()
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/invocations",
                 "raw_path": b"/invocations", "query_string": b"", "root_path": "",
                 "headers": [(HEADER, b"session-a")], "server": ("localhost", 8080),
                 "client": ("localhost", 1234)}
        await asyncio.wait_for(app(scope, receive, send), 20)
        status = next(item["status"] for item in sent if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        return status, body, consumed

    async def test_authorizer_cannot_buffer_oversized_chunked_body(self):
        received = []
        async def authorize(req, session):
            received.append(await req.body())
            return True
        app = self.app(authorize, limit=16)
        status, body, consumed = await self.chunks(app, [b"12345678", b"abcdefghij", b"not-read"])
        self.assertEqual(status, 413)
        self.assertEqual(received, [])
        self.assertEqual(consumed, [b"12345678", b"abcdefghij"])
        self.assertNotIn(b"abcdefghij", body)
        self.assertEqual(app.state.kiro_agentcore.sessions, {})

    async def test_callback_cannot_override_limit_by_catching_error(self):
        seen = []
        async def authorize(req, session):
            try:
                async for chunk in req.stream():
                    seen.append(chunk)
            except Exception:
                return True
            return True
        app = self.app(authorize, limit=8)
        status, _, consumed = await self.chunks(app, [b"1234", b"56789", b"later"])
        self.assertEqual(status, 413)
        self.assertEqual(seen, [b"1234"])
        self.assertEqual(consumed, [b"1234", b"56789"])
        self.assertEqual(app.state.kiro_agentcore.sessions, {})

    async def test_valid_chunked_body_is_cached_for_reauthorization(self):
        checks = []
        async def authorize(req, session):
            checks.append(await req.body())
            return session == "session-a"
        app = self.app(authorize, limit=64, reauthorize=True)
        expected = b'{"prompt":"hello"}'
        status, body, consumed = await self.chunks(app, [expected[:5], expected[5:10], expected[10:]])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["response"], "hello")
        self.assertGreaterEqual(len(checks), 2)
        self.assertTrue(all(item == expected for item in checks))
        self.assertEqual(b"".join(consumed), expected)

    async def test_unread_oversized_body_is_still_rejected(self):
        async def authorize(req, session):
            return True
        app = self.app(authorize, limit=8)
        status, _, consumed = await self.chunks(app, [b"1234", b"56789", b"later"])
        self.assertEqual(status, 413)
        self.assertEqual(consumed, [b"1234", b"56789"])
        self.assertEqual(app.state.kiro_agentcore.sessions, {})

    async def test_decreased_limit_applies_during_authorization(self):
        holder = {}
        async def authorize(req, session):
            service = holder["app"].state.kiro_agentcore
            service.config = replace(service.config, max_body_bytes=8)
            await req.body()
            return True
        app = self.app(authorize, limit=32)
        holder["app"] = app
        status, _, _ = await self.chunks(app, [b"1234", b"56789"])
        self.assertEqual(status, 413)
        self.assertEqual(app.state.kiro_agentcore.sessions, {})

    async def test_increased_limit_cannot_relax_initial_request_limit(self):
        holder = {}
        async def authorize(req, session):
            service = holder["app"].state.kiro_agentcore
            service.config = replace(service.config, max_body_bytes=100)
            await req.body()
            return True
        app = self.app(authorize, limit=8)
        holder["app"] = app
        status, _, _ = await self.chunks(app, [b"1234", b"56789"])
        self.assertEqual(status, 413)
        self.assertEqual(app.state.kiro_agentcore.sessions, {})

    async def test_prebuffered_request_is_checked_before_authorizer(self):
        calls = []
        async def authorize(req, session):
            calls.append(await req.body())
            return True
        app = self.app(authorize, limit=8)
        scope = {"type": "http", "method": "POST", "path": "/invocations",
                 "headers": [(HEADER, b"session-a")]}
        sent = False
        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"123456789", "more_body": False}
            await asyncio.Event().wait()
        req = Request(scope, receive)
        self.assertEqual(await req.body(), b"123456789")
        response = await app.state.kiro_agentcore.invoke(req)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(calls, [])

    async def test_valid_prebuffered_request_remains_available_to_authorizer(self):
        calls = []
        async def authorize(req, session):
            calls.append(await req.body())
            return True
        app = self.app(authorize, limit=32)
        expected = b'{"prompt":"hello"}'
        scope = {"type": "http", "method": "POST", "path": "/invocations",
                 "query_string": b"", "headers": [(HEADER, b"session-a")]}
        sent = False
        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": expected, "more_body": False}
            await asyncio.Event().wait()
        req = Request(scope, receive)
        self.assertEqual(await req.body(), expected)
        response = await app.state.kiro_agentcore.invoke(req)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(calls)
        self.assertTrue(all(body == expected for body in calls))

