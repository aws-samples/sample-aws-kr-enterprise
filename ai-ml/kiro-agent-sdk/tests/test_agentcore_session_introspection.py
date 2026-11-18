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
ACTIONS = ("server_info", "context_usage", "mcp_status")
COMMANDS = [{"name": "review", "description": "Review"}]


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreSessionIntrospectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", cli_path=str(FAKE),
                                      api_key="synthetic", startup_timeout=10, turn_timeout=10,
                                      allow_session_introspection=True)
        self.apps = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, session):
        return req.headers.get("x-test-identity") == "owner" and session == "session-a"

    def app(self, config=None, authorizer=True):
        app = create_agentcore_app(config or self.config,
            options_factory=lambda workspace, resume: KiroAgentOptions(
                tools=[], env={"FAKE_STARTUP_COMMANDS": json.dumps(COMMANDS)}),
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

    async def test_authorized_snapshots_are_session_scoped_and_normalized(self):
        app = self.app()
        for action in ACTIONS:
            self.assertEqual((await self.invoke(app, {"action": action}))[0], 409)
        client = await self.connect(app)
        status, body, headers = await self.invoke(app, {"action": "server_info"})
        self.assertEqual(status, 200)
        self.assertIn("agentCapabilities", json.loads(body)["result"])
        self.assertNotIn("commands", json.loads(body)["result"])
        self.assertEqual(headers[b"cache-control"], b"no-store")
        for action in ("context_usage", "mcp_status"):
            self.assertIsNone(json.loads((await self.invoke(app, {"action": action}))[1])["result"])
        client._context_metadata_by_session["other-session"] = {"contextUsagePercentage": 99}
        client._context_metadata_by_session[client.session_id] = {
            "_meta": {"kiro": {"contextUsage": {"usagePercentage": 12.5},
                               "breakdown": {"tools": {"tokens": 123, "percent": 1.25}}}}}
        client._mcp_status_tracker.update("_kiro/mcp/status", {
            "sessionId": "other-session", "servers": [{"name": "other", "status": "connected"}]})
        client._mcp_status_tracker.update("_kiro/mcp/status", {
            "sessionId": client.session_id, "servers": [{"name": "example", "status": "connecting"}]})
        _, body, _ = await self.invoke(app, {"action": "context_usage"})
        self.assertEqual(json.loads(body)["result"]["percentage"], 12.5)
        self.assertEqual(json.loads(body)["result"]["categories"][0]["tokens"], 123)
        _, body, _ = await self.invoke(app, {"action": "mcp_status"})
        self.assertEqual([(server["name"], server["status"]) for server in
                          json.loads(body)["result"]["mcpServers"]], [("example", "pending")])
        for action in ACTIONS:
            self.assertEqual((await self.invoke(app, {"action": action}, [(b"x-test-identity", b"other")]))[0], 403)
            self.assertEqual((await self.invoke(app, {"action": action}, session=b"session-b"))[0], 403)
            self.assertEqual((await self.invoke(app, {"action": action, "extra": "secret"}))[0], 400)

    async def test_separate_opt_in_and_authorizer_are_required(self):
        disabled = self.app(replace(self.config, allow_session_introspection=False, allow_command_introspection=True))
        await self.connect(disabled)
        for action in ACTIONS:
            self.assertEqual((await self.invoke(disabled, {"action": action}))[0], 403)
        no_authorizer = self.app(replace(self.config, state_dir=self.root / "no-auth"), authorizer=False)
        for action in ACTIONS:
            self.assertEqual((await self.invoke(no_authorizer, {"action": action}))[0], 403)
        enabled = self.app(replace(self.config, state_dir=self.root / "enabled"))
        await self.connect(enabled)
        self.assertEqual((await self.invoke(enabled, {"action": "available_commands"}))[0], 403)
        self.assertEqual((await self.invoke(enabled, {"action": "list_kiro_hooks"}))[0], 403)
        self.assertEqual((await self.invoke(enabled, {"action": "server_info"}))[0], 200)
        both = self.app(replace(self.config, state_dir=self.root / "both", allow_command_introspection=True))
        await self.connect(both)
        _, body, _ = await self.invoke(both, {"action": "server_info"})
        self.assertEqual(json.loads(body)["result"]["commands"], COMMANDS)

    async def test_untrusted_snapshots_do_not_escape_or_keep_the_client(self):
        app = self.app()
        for action, method, result in (("server_info", "get_server_info", ["PRIVATE"]),
                                       ("context_usage", "get_context_usage", {"secret": "PRIVATE", "v": float("nan")}),
                                       ("mcp_status", "get_mcp_status", {"secret": "PRIVATE" * 300000})):
            client = await self.connect(app)
            async def invalid():
                return result
            with patch.object(client, method, invalid), self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
                status, body, _ = await self.invoke(app, {"action": action})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"error": "Introspection failed", "is_error": True})
            self.assertNotIn("PRIVATE", body.decode() + "".join(logs.output))
            self.assertIsNone(app.state.kiro_agentcore.sessions["session-a"].client)

    async def test_changing_policy_discards_inflight_snapshot(self):
        app = self.app()
        client = await self.connect(app)
        service = app.state.kiro_agentcore
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed():
            started.set()
            await release.wait()
            return {"private": "SECRET"}
        with patch.object(client, "get_mcp_status", delayed):
            pending = asyncio.create_task(self.invoke(app, {"action": "mcp_status"}))
            await asyncio.wait_for(started.wait(), 5)
            service.config = replace(self.config, allow_session_introspection=False)
            release.set()
            _, body, _ = await pending
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(b"SECRET", body)
        self.assertIsNone(service.sessions["session-a"].client)

    async def test_session_flag_and_environment_require_strict_booleans(self):
        with self.assertRaises(ValueError):
            replace(self.config, allow_session_introspection="true")
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_SESSION_INTROSPECTION": "true"}, clear=True):
            self.assertTrue(AgentCoreConfig.from_env().allow_session_introspection)
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_SESSION_INTROSPECTION": "1"}, clear=True):
            with self.assertRaises(ValueError):
                AgentCoreConfig.from_env()
