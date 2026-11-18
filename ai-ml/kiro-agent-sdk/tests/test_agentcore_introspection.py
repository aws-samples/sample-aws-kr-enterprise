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
    from kiro_agent_sdk import KiroAgentOptions
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app

OWNER = [(b"x-test-identity", b"owner")]
COMMANDS = [{"name": "review", "description": "Review", "input": {"hint": "[path]"}}]


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreIntrospectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", cli_path=str(FAKE),
            api_key="synthetic", startup_timeout=10, turn_timeout=10,
            allow_command_introspection=True, allow_hook_introspection=True)
        self.apps = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, session):
        return req.headers.get("x-test-identity") == "owner" and session == "session-a"

    def app(self, config=None, authorizer=True, factory=None):
        if factory is None:
            factory = lambda workspace, resume: KiroAgentOptions(tools=[], env={
                "FAKE_LOG_REQUESTS": str(self.log), "FAKE_HOOK_EVENTS": "1",
                "FAKE_STARTUP_COMMANDS": json.dumps(COMMANDS)})
        app = create_agentcore_app(config or self.config, options_factory=factory,
                                   session_authorizer=self.authorize if authorizer else None)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, identity=OWNER, session=b"session-a"):
        return await request(app, "POST", "/invocations", json.dumps(payload).encode(),
                             [(HEADER, session), *identity])

    async def connect(self, app):
        status, body, _ = await self.invoke(app, {"prompt": "hello"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        return app.state.kiro_agentcore.sessions["session-a"].client

    async def test_flags_auth_session_isolation_and_listing(self):
        disabled = self.app(replace(self.config, allow_command_introspection=False,
                                    allow_hook_introspection=False))
        for action in ("available_commands", "list_kiro_hooks"):
            self.assertEqual((await self.invoke(disabled, {"action": action}))[0], 403)
        no_authorizer = self.app(replace(self.config, state_dir=self.root / "no-auth"), authorizer=False)
        self.assertEqual((await self.invoke(no_authorizer, {"action": "available_commands"}))[0], 403)
        app = self.app(replace(self.config, state_dir=self.root / "enabled"))
        self.assertEqual((await self.invoke(app, {"action": "available_commands"}))[0], 409)
        await self.connect(app)
        status, body, headers = await self.invoke(app, {"action": "available_commands"})
        self.assertEqual((status, json.loads(body)), (200, {"action": "available_commands",
                                                          "result": COMMANDS, "is_error": False}))
        self.assertEqual(headers[b"cache-control"], b"no-store")
        status, body, _ = await self.invoke(app, {"action": "list_kiro_hooks", "trigger": "PreToolUse",
            "tool_id": "read", "tool_tags": ["fs"], "include_disabled": True})
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in json.loads(body)["result"]["hooks"]], ["hook-1"])
        for action in ("available_commands", "list_kiro_hooks"):
            self.assertEqual((await self.invoke(app, {"action": action}, [(b"x-test-identity", b"other")]))[0], 403)
            self.assertEqual((await self.invoke(app, {"action": action}, session=b"session-b"))[0], 403)
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        listing = [item for item in records if item.get("method") == "_kiro/hooks/list"][-1]
        self.assertEqual(listing["params"]["trigger"], "PreToolUse")
        self.assertEqual(listing["params"]["toolId"], "read")
        self.assertEqual(listing["params"]["toolTags"], ["fs"])
        self.assertIs(listing["params"]["includeDisabled"], True)

    async def test_invalid_filters_do_not_query_or_broaden(self):
        app = self.app()
        await self.connect(app)
        before = self.log.read_text().count('"_kiro/hooks/list"')
        invalid = [{"trigger": None}, {"trigger": "bad"}, {"tool_id": "bad\n"},
                   {"tool_tags": ["x"]}, {"tool_id": "read", "tool_tags": ["x", "x"]},
                   {"include_disabled": 1}, {"include_disabled": None}, {"other": "secret"}]
        for filters in invalid:
            with self.subTest(filters=filters):
                self.assertEqual((await self.invoke(app, {"action": "list_kiro_hooks", **filters}))[0], 400)
        self.assertEqual((await self.invoke(app, {"action": "available_commands", "extra": 1}))[0], 400)
        self.assertEqual(before, self.log.read_text().count('"_kiro/hooks/list"'))
        self.assertEqual((await self.invoke(app, {"action": "list_kiro_hooks"}))[0], 200)

    async def test_busy_sessions_are_rejected(self):
        app = self.app()
        await self.connect(app)
        session = app.state.kiro_agentcore.sessions["session-a"]
        for lock in (session.lock, session.control_lock):
            async with lock:
                for action in ("available_commands", "list_kiro_hooks"):
                    self.assertEqual((await self.invoke(app, {"action": action}))[0], 409)
        self.assertEqual((await self.invoke(app, {"action": "available_commands"}))[0], 200)

    async def test_command_and_hook_opt_ins_are_independent(self):
        command = self.app(replace(self.config, allow_hook_introspection=False))
        await self.connect(command)
        self.assertEqual((await self.invoke(command, {"action": "available_commands"}))[0], 200)
        self.assertEqual((await self.invoke(command, {"action": "list_kiro_hooks"}))[0], 403)
        hook = self.app(replace(self.config, state_dir=self.root / "hook-only", allow_command_introspection=False))
        await self.connect(hook)
        self.assertEqual((await self.invoke(hook, {"action": "available_commands"}))[0], 403)
        self.assertEqual((await self.invoke(hook, {"action": "list_kiro_hooks"}))[0], 200)

    async def test_policy_change_while_waiting_for_pool_lock_does_not_query(self):
        app = self.app()
        client = await self.connect(app)
        service = app.state.kiro_agentcore
        called = False
        async def listing():
            nonlocal called
            called = True
            return []
        async with service.pool_lock:
            with patch.object(client, "get_kiro_available_commands", listing):
                pending = asyncio.create_task(self.invoke(app, {"action": "available_commands"}))
                async def wait_for_pool_waiter():
                    while not service.pool_lock._waiters:
                        await asyncio.sleep(0)
                await asyncio.wait_for(wait_for_pool_waiter(), 5)
                service.config = replace(self.config, allow_command_introspection=False)
                # Keep the mock active until the waiting control has returned.
                service.pool_lock.release()
                try:
                    status, _, _ = await pending
                finally:
                    await service.pool_lock.acquire()
        self.assertEqual(status, 403)
        self.assertFalse(called)

    async def test_malformed_and_oversized_results_are_private_and_discarded(self):
        app = self.app()
        cyclic = {}; cyclic["self"] = cyclic
        for value in ([{"name": "PRIVATE_VALUE", "text": "x" * (1024 * 1024 + 1)}],
                      [{"name": "PRIVATE_VALUE", "number": float("nan")}],
                      [cyclic], [{"name": "PRIVATE_VALUE", "bad": object()}]):
            with self.subTest(kind=type(value[0]).__name__):
                client = await self.connect(app)
                async def poisoned():
                    return value
                with patch.object(client, "get_kiro_available_commands", poisoned):
                    with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
                        status, body, _ = await self.invoke(app, {"action": "available_commands"})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"error": "Introspection failed", "is_error": True})
                self.assertNotIn("PRIVATE_VALUE", body.decode() + "".join(logs.output))
                self.assertIsNone(app.state.kiro_agentcore.sessions["session-a"].client)

    async def test_timeout_discards_late_result_and_allows_new_session(self):
        app = self.app()
        client = await self.connect(app)
        service = app.state.kiro_agentcore
        service.config = replace(self.config, startup_timeout=0.05)
        release, finished = asyncio.Event(), asyncio.Event()
        async def slow():
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return [{"name": "PRIVATE_LATE"}]
        try:
            with patch.object(client, "get_kiro_available_commands", slow):
                status, body, _ = await self.invoke(app, {"action": "available_commands"})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["is_error"])
            self.assertNotIn(b"PRIVATE_LATE", body)
            self.assertIsNone(service.sessions["session-a"].client)
            service.config = self.config
            await self.connect(app)
            release.set()
            await asyncio.wait_for(finished.wait(), 5)
            _, body, _ = await self.invoke(app, {"action": "available_commands"})
            self.assertFalse(json.loads(body)["is_error"])
            self.assertNotIn(b"PRIVATE_LATE", body)
        finally:
            release.set()

    async def test_policy_change_during_query_does_not_expose_result(self):
        app = self.app()
        client = await self.connect(app)
        service = app.state.kiro_agentcore
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed():
            started.set()
            await release.wait()
            return [{"name": "PRIVATE_RESULT"}]
        with patch.object(client, "get_kiro_available_commands", delayed):
            pending = asyncio.create_task(self.invoke(app, {"action": "available_commands"}))
            await asyncio.wait_for(started.wait(), 5)
            service.config = replace(self.config, allow_command_introspection=False)
            release.set()
            status, body, _ = await pending
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(b"PRIVATE_RESULT", body)
        self.assertIsNone(service.sessions["session-a"].client)

    async def test_authorizer_change_during_query_does_not_expose_result(self):
        app = self.app()
        client = await self.connect(app)
        service = app.state.kiro_agentcore
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed():
            started.set()
            await release.wait()
            return [{"name": "PRIVATE_RESULT"}]
        with patch.object(client, "get_kiro_available_commands", delayed):
            pending = asyncio.create_task(self.invoke(app, {"action": "available_commands"}))
            await asyncio.wait_for(started.wait(), 5)
            service.session_authorizer = self.authorize
            release.set()
            status, body, _ = await pending
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(b"PRIVATE_RESULT", body)
        self.assertIsNone(service.sessions["session-a"].client)

    async def test_cancelled_query_releases_session_and_discards_client(self):
        app = self.app()
        client = await self.connect(app)
        started, finished = asyncio.Event(), asyncio.Event()
        async def delayed():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        with patch.object(client, "get_kiro_available_commands", delayed):
            pending = asyncio.create_task(self.invoke(app, {"action": "available_commands"}))
            await asyncio.wait_for(started.wait(), 5)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(finished.wait(), 5)
        session = app.state.kiro_agentcore.sessions["session-a"]
        self.assertFalse(session.lock.locked())
        self.assertIsNone(session.client)
        await self.connect(app)

    async def test_authorizer_change_while_reading_body_is_denied(self):
        app = self.app(authorizer=False)
        started, release = asyncio.Event(), asyncio.Event()
        sent = []
        async def receive():
            started.set()
            await release.wait()
            return {"type": "http.request", "body": b'{"action":"available_commands"}', "more_body": False}
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": "/invocations", "raw_path": b"/invocations", "query_string": b"",
                 "root_path": "", "headers": [(HEADER, b"session-a")], "server": ("localhost", 8080),
                 "client": ("localhost", 1)}
        pending = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(started.wait(), 5)
        app.state.kiro_agentcore.session_authorizer = self.authorize
        release.set()
        await asyncio.wait_for(pending, 5)
        self.assertEqual(next(item["status"] for item in sent if item["type"] == "http.response.start"), 403)

    async def test_config_flags_require_strict_booleans(self):
        for field in ("allow_command_introspection", "allow_hook_introspection"):
            with self.assertRaises(ValueError):
                replace(self.config, **{field: "true"})
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_COMMAND_INTROSPECTION": "true",
                                     "KIRO_AGENTCORE_ALLOW_HOOK_INTROSPECTION": "true"}, clear=True):
            parsed = AgentCoreConfig.from_env()
            self.assertTrue(parsed.allow_command_introspection and parsed.allow_hook_introspection)
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_HOOK_INTROSPECTION": "1"}, clear=True):
            with self.assertRaises(ValueError):
                AgentCoreConfig.from_env()
