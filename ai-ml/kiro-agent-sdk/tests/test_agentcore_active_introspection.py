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
COMMANDS = [{"name": "review", "description": "Review"}]
ACTIONS = ("available_commands", "server_info", "context_usage", "mcp_status")


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreActiveIntrospectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.config = AgentCoreConfig(allow_unauthorized_sessions=True,
            state_dir=self.root / "state", cli_path=str(FAKE), api_key="synthetic",
            startup_timeout=10, turn_timeout=20, allow_active_introspection=True,
            allow_session_introspection=True, allow_command_introspection=True,
            allow_hook_introspection=True)
        self.apps = []
        self.turns = []

    async def asyncTearDown(self):
        for app, task in self.turns:
            if not task.done():
                await self.invoke(app, {"action": "interrupt"})
                await asyncio.wait_for(task, 10)
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, session):
        return req.headers.get("x-test-identity") == "owner" and session == "session-a"

    def app(self, config=None, authorizer=True):
        app = create_agentcore_app(config or self.config,
            options_factory=lambda workspace, resume: KiroAgentOptions(tools=[], env={
                "FAKE_LOG_REQUESTS": str(self.log), "FAKE_STARTUP_COMMANDS": json.dumps(COMMANDS),
                "FAKE_HOOK_EVENTS": "1"}), session_authorizer=self.authorize if authorizer else None)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, identity=OWNER, session=b"session-a"):
        return await request(app, "POST", "/invocations", json.dumps(payload).encode(),
                             [(HEADER, session), *identity])

    async def start_turn(self, app, *, stream=False):
        payload = {"prompt": "cancel"}
        if stream:
            payload["stream"] = True
        task = asyncio.create_task(self.invoke(app, payload))
        self.turns.append((app, task))
        async def wait_until_running():
            while True:
                session = app.state.kiro_agentcore.sessions.get("session-a")
                if (session and session.turn_ready and session.client and session.client._turn and
                        not session.client._turn.done()):
                    return session
                if task.done():
                    raise AssertionError("Synthetic turn ended before it was ready")
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(wait_until_running(), 10)

    def install_snapshots(self, client):
        client._context_metadata_by_session["other-session"] = {"contextUsagePercentage": 99}
        client._context_metadata_by_session[client.session_id] = {
            "_meta": {"kiro": {"contextUsage": {"usagePercentage": 12.5},
                               "breakdown": {"tools": {"tokens": 123, "percent": 1.25}}}}}
        tracker = client._mcp_status_tracker
        tracker.update("_kiro/mcp/status", {"sessionId": "other-session", "servers": [
            {"name": "other", "status": "connected"}]})
        tracker.update("_kiro/mcp/status", {"sessionId": client.session_id, "servers": [
            {"name": "example", "status": "connecting"}]})

    async def stop_turn(self, app, session):
        self.assertEqual((await self.invoke(app, {"action": "interrupt"}))[0], 200)
        task = next(task for entry, task in self.turns if entry is app)
        self.assertEqual((await asyncio.wait_for(task, 10))[0], 200)
        self.assertIs(session.client, app.state.kiro_agentcore.sessions["session-a"].client)

    async def test_active_cached_snapshots_match_sdk_without_extra_rpc(self):
        app = self.app()
        for action in ACTIONS:
            self.assertEqual((await self.invoke(app, {"action": action}))[0], 409)
        session = await self.start_turn(app)
        client = session.client
        self.install_snapshots(client)
        expected = {"available_commands": await client.get_kiro_available_commands(),
                    "server_info": await client.get_server_info(),
                    "context_usage": await client.get_context_usage(), "mcp_status": await client.get_mcp_status()}
        async def wait_for_prompt_record():
            while not self.log.exists() or '"method": "session/prompt"' not in self.log.read_text():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_prompt_record(), 10)
        before = self.log.read_text()
        for action in ACTIONS:
            status, body, headers = await self.invoke(app, {"action": action})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"action": action, "result": expected[action], "is_error": False})
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertEqual((await self.invoke(app, {"action": action}, [(b"x-test-identity", b"other")]))[0], 403)
            self.assertEqual((await self.invoke(app, {"action": action}, session=b"session-b"))[0], 403)
        self.assertEqual(self.log.read_text(), before)
        for action in ("list_kiro_hooks",):
            self.assertEqual((await self.invoke(app, {"action": action}))[0], 409)
        self.assertEqual(self.log.read_text(), before)
        await self.stop_turn(app, session)

    async def test_requires_independent_flags_and_never_uses_callbacks(self):
        config = replace(self.config, allow_active_introspection=False)
        app = self.app(config)
        session = await self.start_turn(app)
        client, service = session.client, app.state.kiro_agentcore
        self.install_snapshots(client)
        for action in ACTIONS:
            self.assertEqual((await self.invoke(app, {"action": action}))[0], 409)
        service.config = replace(config, allow_active_introspection=True, allow_command_introspection=False)
        self.assertEqual((await self.invoke(app, {"action": "available_commands"}))[0], 403)
        _, body, _ = await self.invoke(app, {"action": "server_info"})
        self.assertNotIn("commands", json.loads(body)["result"])
        service.config = replace(config, allow_active_introspection=True, allow_session_introspection=False)
        for action in ("server_info", "context_usage", "mcp_status"):
            self.assertEqual((await self.invoke(app, {"action": action}))[0], 403)
        service.config = self.config
        called = []
        async def forbidden():
            called.append(True)
            raise AssertionError("Active snapshots must not call application callbacks")
        with patch.object(client, "get_server_info", forbidden), patch.object(client, "get_context_usage", forbidden), \
             patch.object(client, "get_mcp_status", forbidden), patch.object(client, "get_kiro_available_commands", forbidden):
            for action in ACTIONS:
                self.assertEqual((await self.invoke(app, {"action": action}))[0], 200)
        self.assertEqual(called, [])
        await self.stop_turn(app, session)

    async def test_active_snapshots_require_a_session_authorizer(self):
        app = self.app(authorizer=False)
        session = await self.start_turn(app)
        for action in ACTIONS:
            self.assertEqual((await self.invoke(app, {"action": action}))[0], 403)
        await self.stop_turn(app, session)

    async def test_invalid_snapshots_are_private_and_do_not_interrupt_the_turn(self):
        app = self.app()
        session = await self.start_turn(app)
        client = session.client
        original = client.server_info
        cyclic = {}; cyclic["self"] = cyclic
        for value in ({"secret": "PRIVATE" * 200000}, {"secret": "PRIVATE", "value": float("nan")}, cyclic):
            client.server_info = value
            with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
                status, body, _ = await self.invoke(app, {"action": "server_info"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"error": "Introspection failed", "is_error": True})
            self.assertNotIn("PRIVATE", body.decode() + "".join(logs.output))
            self.assertIs(session.client, client)
            self.assertFalse(client._turn.done())
        client.server_info = original
        self.assertEqual((await self.invoke(app, {"action": "server_info"}))[0], 200)
        await self.stop_turn(app, session)
        self.assertFalse(json.loads((await self.invoke(app, {"prompt": "after"}))[1])["is_error"])

    async def test_bounded_snapshots_do_not_execute_custom_object_methods(self):
        app = self.app()
        session = await self.start_turn(app)
        client = session.client
        called = []
        class EvilDict(dict):
            def items(self):
                called.append("items")
                raise AssertionError("must not execute")
            def __deepcopy__(self, memo):
                called.append("copy")
                raise AssertionError("must not execute")
        original = client.server_info
        client.server_info = {"payload": EvilDict({"private": "SECRET"})}
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
            status, body, _ = await self.invoke(app, {"action": "server_info"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(b"SECRET", body)
        self.assertEqual(called, [])
        client.server_info = original
        await self.stop_turn(app, session)

    async def test_oversized_context_and_mcp_cache_do_not_abort_the_turn(self):
        app = self.app()
        session = await self.start_turn(app)
        client = session.client
        client._context_metadata_by_session[client.session_id] = {"private": "SECRET" * 200000}
        client._mcp_status_tracker.update("_kiro/mcp/status", {
            "sessionId": client.session_id, "servers": [], "private": "SECRET" * 200000})
        for action in ("context_usage", "mcp_status"):
            with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
                status, body, _ = await self.invoke(app, {"action": action})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["is_error"])
            self.assertNotIn("SECRET", body.decode() + "".join(logs.output))
            self.assertIs(session.client, client)
            self.assertFalse(client._turn.done())
        await self.stop_turn(app, session)

    async def test_active_control_lock_and_unready_sessions_are_rejected(self):
        app = self.app()
        session = await self.start_turn(app)
        async with session.control_lock:
            for action in ACTIONS:
                self.assertEqual((await self.invoke(app, {"action": action}))[0], 409)
        session.turn_ready = False
        try:
            self.assertEqual((await self.invoke(app, {"action": "server_info"}))[0], 409)
        finally:
            session.turn_ready = True
        await self.stop_turn(app, session)

    async def test_policy_change_while_waiting_does_not_expose_snapshot(self):
        app = self.app()
        session = await self.start_turn(app)
        service = app.state.kiro_agentcore
        async with service.pool_lock:
            pending = asyncio.create_task(self.invoke(app, {"action": "server_info"}))
            async def wait_for_pool_waiter():
                while not service.pool_lock._waiters:
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_for_pool_waiter(), 5)
            service.config = replace(self.config, allow_active_introspection=False)
            service.pool_lock.release()
            try:
                status, body, _ = await pending
            finally:
                await service.pool_lock.acquire()
        self.assertEqual(status, 403)
        self.assertNotIn(b"agentCapabilities", body)
        service.config = self.config
        await self.stop_turn(app, session)

    async def test_parallel_snapshots_do_not_block_an_interrupt(self):
        app = self.app()
        session = await self.start_turn(app)
        self.install_snapshots(session.client)
        snapshots = [asyncio.create_task(self.invoke(app, {"action": ACTIONS[index % len(ACTIONS)]}))
                     for index in range(24)]
        interrupt = asyncio.create_task(self.invoke(app, {"action": "interrupt"}))
        responses = await asyncio.wait_for(asyncio.gather(*snapshots, interrupt), 10)
        self.assertEqual(responses[-1][0], 200)
        for status, body, _ in responses[:-1]:
            self.assertIn(status, (200, 409))
            if status == 200:
                self.assertFalse(json.loads(body)["is_error"])
        task = next(task for entry, task in self.turns if entry is app)
        self.assertEqual((await asyncio.wait_for(task, 10))[0], 200)
        self.assertFalse(session.lock.locked())

    async def test_cached_snapshots_are_available_during_a_streaming_turn(self):
        app = self.app()
        session = await self.start_turn(app, stream=True)
        self.install_snapshots(session.client)
        _, body, _ = await self.invoke(app, {"action": "context_usage"})
        self.assertEqual(json.loads(body)["result"]["percentage"], 12.5)
        await self.stop_turn(app, session)
        task = next(task for entry, task in self.turns if entry is app)
        self.assertIn(b'"type": "result"', (await task)[1])

    async def test_v2_active_snapshots_use_reported_v2_metadata(self):
        app = self.app(replace(self.config, agent_engine="v2", allow_hook_introspection=False))
        session = await self.start_turn(app)
        client = session.client
        client._context_metadata_by_session[client.session_id] = {"contextUsagePercentage": 7.25}
        client._mcp_status_tracker.update("_kiro.dev/mcp/server_initialized", {
            "sessionId": client.session_id, "serverName": "example"})
        _, body, _ = await self.invoke(app, {"action": "context_usage"})
        self.assertEqual(json.loads(body)["result"]["percentage"], 7.25)
        _, body, _ = await self.invoke(app, {"action": "mcp_status"})
        self.assertEqual(json.loads(body)["result"]["mcpServers"], [
            {"name": "example", "status": "connected"}])
        await self.stop_turn(app, session)

    async def test_active_flag_is_strict_and_default_is_disabled(self):
        self.assertFalse(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "default", api_key="synthetic").allow_active_introspection)
        with self.assertRaises(ValueError):
            replace(self.config, allow_active_introspection="true")
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_ACTIVE_INTROSPECTION": "true"}, clear=True):
            self.assertTrue(AgentCoreConfig.from_env().allow_active_introspection)
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_ACTIVE_INTROSPECTION": "1"}, clear=True):
            with self.assertRaises(ValueError):
                AgentCoreConfig.from_env()
