import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request

if HAS_STARLETTE:
    from kiro_agent_sdk import InMemorySessionStore, KiroAgentOptions, SQLiteSessionStore
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
    from kiro_agent_sdk.archive import _archive_payload


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreSessionHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.apps = []
        self.allowed = True

    async def asyncTearDown(self):
        self.allowed = True
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, request, session):
        return self.allowed and request.headers.get("x-test-identity") == "owner" and session == "session-a"

    def app(self, store, *, enabled=True, tools=False, authorizer=True):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / f"state-{len(self.apps)}", cli_path=str(FAKE),
                                 api_key="synthetic", allow_session_history=enabled,
                                 allow_session_history_tools=tools)
        app = create_agentcore_app(
            config, options_factory=lambda workspace, resume: KiroAgentOptions(tools=[], session_store=store),
            session_authorizer=self.authorize if authorizer else None)
        self.apps.append(app)
        return app

    async def invoke(self, app, data, *, identity=b"owner", session=b"session-a"):
        return await request(app, "POST", "/invocations", json.dumps(data).encode(),
                             [(HEADER, session), (b"x-test-identity", identity)])

    async def connect(self, app):
        status, body, _ = await self.invoke(app, {"prompt": "synthetic question"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        return app.state.kiro_agentcore.sessions["session-a"]

    async def test_both_stores_provide_session_scoped_pages(self):
        for store in (InMemorySessionStore(), SQLiteSessionStore(self.root / "mirror.db")):
            app = self.app(store)
            session = await self.connect(app)
            await self.invoke(app, {"prompt": "second question"})
            status, body, headers = await self.invoke(app, {"action": "session_messages", "limit": 1})
            first = json.loads(body)["result"]
            self.assertEqual(status, 200)
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertEqual((first["messages"][0]["type"], first["messages"][0]["content"]),
                             ("UserMessage", [{"text": "synthetic question"}]))
            self.assertEqual(first["next_offset"], 1)
            self.assertEqual(first["messages"][0]["session_id"], session.client.session_id)
            status, body, _ = await self.invoke(app, {"action": "session_messages", "limit": 50, "offset": 1})
            self.assertEqual(status, 200)
            self.assertIn("second question", body.decode())
            self.assertIsNone(json.loads(body)["result"]["next_offset"])
            self.assertEqual((await self.invoke(app, {"action": "session_messages"}, identity=b"other"))[0], 403)
            self.assertEqual((await self.invoke(app, {"action": "session_messages"}, session=b"other"))[0], 403)

    async def test_opt_in_restrictions_and_no_arbitrary_keys(self):
        store = InMemorySessionStore()
        disabled = self.app(store, enabled=False)
        await self.connect(disabled)
        self.assertEqual((await self.invoke(disabled, {"action": "session_messages"}))[0], 403)
        no_authorizer = self.app(store, authorizer=False)
        self.assertEqual((await self.invoke(no_authorizer, {"action": "session_messages"}))[0], 403)
        app = self.app(store)
        session = await self.connect(app)
        for extra in ({"session_id": "other"}, {"directory": "/tmp"}, {"limit": True},
                      {"limit": 51}, {"offset": -1}, {"offset": 20001}, {"coalesce_chunks": 1}):
            self.assertEqual((await self.invoke(app, {"action": "session_messages", **extra}))[0], 400)
        self.assertEqual((await self.invoke(app, {"action": "session_messages",
                                                  "include_observed_tool_messages": True}))[0], 403)
        session.client._store_key["session_id"] = "other"
        self.assertEqual((await self.invoke(app, {"action": "session_messages"}))[0], 400)

    async def test_tool_outputs_need_second_opt_in(self):
        store = InMemorySessionStore()
        app = self.app(store, tools=True)
        session = await self.connect(app)
        prompt_key = "a" * 32
        await store.append(session.client._store_key, [
            {"type": "prompt", "kiro_sdk_prompt_key": prompt_key, "data": [{"type": "text", "text": "tool"}]},
            {"type": "update", "kiro_sdk_prompt_key": prompt_key,
             "data": {"sessionUpdate": "tool_call", "toolCallId": "call", "title": "probe",
                      "rawInput": {"value": "PRIVATE-INPUT"}}},
            {"type": "update", "kiro_sdk_prompt_key": prompt_key,
             "data": {"sessionUpdate": "tool_call_update", "toolCallId": "call", "status": "completed",
                      "rawOutput": "PRIVATE-OUTPUT"}},
            {"type": "result", "kiro_sdk_prompt_key": prompt_key, "data": {}},
        ])
        _, normal, _ = await self.invoke(app, {"action": "session_messages"})
        self.assertNotIn(b"PRIVATE-INPUT", normal)
        self.assertNotIn(b"PRIVATE-OUTPUT", normal)
        _, tools, _ = await self.invoke(app, {"action": "session_messages", "include_observed_tool_messages": True})
        self.assertIn(b"PRIVATE-INPUT", tools)
        self.assertIn(b"PRIVATE-OUTPUT", tools)

    async def test_history_skips_large_archive_data_without_skipping_messages(self):
        for store in (InMemorySessionStore(), SQLiteSessionStore(self.root / "large-archive.db")):
            app = self.app(store)
            session = await self.connect(app)
            payload = _archive_payload(session.client.session_id, b"x" * 6300000)
            await store.append(session.client._store_key, [{"type": "session_archive", "data": payload}])
            _, body, _ = await self.invoke(app, {"action": "session_messages"})
            result = json.loads(body)
            self.assertFalse(result["is_error"])
            self.assertIn("synthetic question", body.decode())
            self.assertNotIn(b"base64", body)

    async def test_revocation_during_read_hides_data(self):
        store = InMemorySessionStore()
        app = self.app(store)
        await self.connect(app)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.load_bounded
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        with patch.object(store, "load_bounded", delayed):
            pending = asyncio.create_task(self.invoke(app, {"action": "session_messages"}))
            await asyncio.wait_for(entered.wait(), 5)
            self.allowed = False
            release.set()
            status, body, _ = await pending
        self.assertEqual(status, 403)
        self.assertNotIn(b"synthetic question", body)

    async def test_revocation_after_response_headers_hides_body(self):
        app = self.app(InMemorySessionStore())
        await self.connect(app)
        incoming = [{"type": "http.request", "body": b'{"action":"session_messages"}', "more_body": False}]
        sent = []
        async def receive():
            if incoming:
                return incoming.pop(0)
            await asyncio.Event().wait()
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                self.allowed = False
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/invocations",
                 "raw_path": b"/invocations", "query_string": b"",
                 "headers": [(HEADER, b"session-a"), (b"x-test-identity", b"owner")],
                 "server": ("localhost", 8080), "client": ("localhost", 1)}
        await asyncio.wait_for(app(scope, receive, send), 10)
        body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        self.assertNotIn(b"synthetic question", body)
        self.assertEqual(json.loads(body)["error"], "Session access denied")
        self.assertFalse(app.state.kiro_agentcore.sessions["session-a"].lock.locked())

    async def test_store_change_and_busy_session_prevent_snapshot(self):
        store = InMemorySessionStore()
        app = self.app(store)
        session = await self.connect(app)
        await session.lock.acquire()
        try:
            self.assertEqual((await self.invoke(app, {"action": "session_messages"}))[0], 409)
        finally:
            session.lock.release()
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.load_bounded
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        with patch.object(store, "load_bounded", delayed):
            pending = asyncio.create_task(self.invoke(app, {"action": "session_messages"}))
            await asyncio.wait_for(entered.wait(), 5)
            session.client.options.session_store = InMemorySessionStore()
            release.set()
            _, body, _ = await pending
        self.assertNotIn(b"synthetic question", body)
        self.assertIn("error", json.loads(body))

    async def test_oversized_history_returns_only_a_sanitized_error(self):
        store = InMemorySessionStore()
        app = self.app(store)
        session = await self.connect(app)
        await store.append(session.client._store_key, [{"type": "prompt", "data": "PRIVATE" * 1300000}])
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
            _, body, _ = await self.invoke(app, {"action": "session_messages", "limit": 1})
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(b"PRIVATE", body)
        self.assertNotIn("PRIVATE", "".join(logs.output))

    async def test_timed_out_reads_stay_counted_until_the_store_finishes(self):
        store = InMemorySessionStore()
        app = self.app(store)
        await self.connect(app)
        service = app.state.kiro_agentcore
        service.config = replace(service.config, startup_timeout=0.1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return [{"type": "prompt", "data": [{"type": "text", "text": "PRIVATE"}]}]
        try:
            with patch.object(store, "load_bounded", delayed):
                with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
                    pending = asyncio.create_task(self.invoke(app, {"action": "session_messages"}))
                    await asyncio.wait_for(entered.wait(), 5)
                    _, body, _ = await pending
                self.assertNotIn(b"PRIVATE", body)
                self.assertTrue(json.loads(body)["is_error"])
                self.assertEqual(len(service._session_history_operations), 1)
                release.set()
                for _ in range(100):
                    if not service._session_history_operations:
                        break
                    await asyncio.sleep(0.01)
                self.assertFalse(service._session_history_operations)
        finally:
            release.set()

    async def test_missing_mirror_is_an_error_instead_of_empty_history(self):
        store = InMemorySessionStore()
        app = self.app(store)
        session = await self.connect(app)
        await store.delete(session.client._store_key)
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
            _, body, _ = await self.invoke(app, {"action": "session_messages"})
        self.assertTrue(json.loads(body)["is_error"])

    async def test_history_capacity_does_not_consume_background_archive_capacity(self):
        store = InMemorySessionStore()
        app = self.app(store)
        await self.connect(app)
        service = app.state.kiro_agentcore
        pending = [asyncio.get_running_loop().create_future() for _ in range(8)]
        service._session_history_operations.update(pending)
        called = False
        async def unexpected(*args, **kwargs):
            nonlocal called
            called = True
            return []
        try:
            with patch.object(store, "load_bounded", unexpected):
                with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
                    _, body, _ = await self.invoke(app, {"action": "session_messages"})
            self.assertTrue(json.loads(body)["is_error"])
            self.assertFalse(called)
            self.assertFalse(service._background_archive_operations)
        finally:
            service._session_history_operations.clear()
            for item in pending:
                item.cancel()

    def test_strict_flags(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, api_key="synthetic")
        with self.assertRaises(ValueError):
            replace(config, allow_session_history="true")
        with self.assertRaises(ValueError):
            replace(config, allow_session_history_tools=True)
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_SESSION_HISTORY": "true",
                                     "KIRO_AGENTCORE_ALLOW_SESSION_HISTORY_TOOLS": "true"}, clear=True):
            parsed = AgentCoreConfig.from_env()
        self.assertTrue(parsed.allow_session_history and parsed.allow_session_history_tools)
