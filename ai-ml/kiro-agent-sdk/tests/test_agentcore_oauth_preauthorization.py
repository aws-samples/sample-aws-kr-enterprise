import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, McpOAuthConfig

if importlib.util.find_spec("starlette") and importlib.util.find_spec("mcp"):
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
    from test_agentcore import FAKE, HEADER, request
    from test_mcp_oauth import OAuthFixture


@unittest.skipUnless(importlib.util.find_spec("starlette") and importlib.util.find_spec("mcp"),
                     "requires AgentCore and MCP")
class AgentCoreOAuthPreauthorizationTests(unittest.IsolatedAsyncioTestCase):
    def test_preauthorization_requires_oauth_interactions(self):
        with self.assertRaisesRegex(ValueError, "requires MCP OAuth interactions"):
            AgentCoreConfig(allow_unauthorized_sessions=True, api_key="fixture", preauthorize_mcp_oauth=True)
        environment = {"KIRO_AGENTCORE_AUTH_MODE": "cli", "KIRO_AGENTCORE_ALLOW_INTERACTIONS": "true",
                       "KIRO_AGENTCORE_ALLOW_MCP_OAUTH_INTERACTIONS": "true",
                       "KIRO_AGENTCORE_PREAUTHORIZE_MCP_OAUTH": "true"}
        with patch.dict(os.environ, environment, clear=True):
            self.assertTrue(AgentCoreConfig.from_env().preauthorize_mcp_oauth)
        key = "agent:plugin-test-agent:server"
        original = {key: {"type": "http", "url": "https://example.com/mcp?fixture-private"}}
        config = AgentCoreConfig(allow_unauthorized_sessions=True, api_key="fixture", allow_interactions=True, allow_mcp_oauth_interactions=True,
                                 preauthorize_mcp_oauth=True, mcp_oauth_preauthorize_servers=original)
        original[key]["url"] = "https://other.example.com/mcp"
        self.assertEqual(config.mcp_oauth_preauthorize_servers[key]["url"],
                         "https://example.com/mcp?fixture-private")
        self.assertNotIn("fixture-private", repr(config))
        with self.assertRaises(ValueError):
            AgentCoreConfig(allow_unauthorized_sessions=True, api_key="fixture", allow_interactions=True, allow_mcp_oauth_interactions=True,
                preauthorize_mcp_oauth=True,
                mcp_oauth_preauthorize_servers={key: {"type": "http", "url": "http://example.com/mcp"}})

    async def test_owner_authorizes_before_kiro_starts_and_other_user_is_denied(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        with tempfile.TemporaryDirectory() as directory:
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), cli_path=str(FAKE), api_key="fixture",
                allow_interactions=True, allow_mcp_oauth_interactions=True, preauthorize_mcp_oauth=True,
                startup_timeout=10, turn_timeout=10, interaction_timeout=5)
            async def owner(req, session_id):
                return req.headers.get("x-owner") == "yes"
            def options(workspace, resume):
                return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                    mcp_servers={"probe": {"type": "http", "url": url}},
                    mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
            app = create_agentcore_app(config, options_factory=options, session_authorizer=owner)
            original_connect = KiroSDKClient.connect
            connected = []

            async def connect(client, *args, **kwargs):
                connected.append(list(fixture.token_grants))
                result = await original_connect(client, *args, **kwargs)
                client._mcp_status_tracker.update("_kiro/mcp/status", {
                    "sessionId": client.session_id, "servers": [{"name": "probe", "status": "connected"}]})
                return result

            headers = [(HEADER, b"session-a"), (b"x-owner", b"yes")]
            other = [(HEADER, b"session-a"), (b"x-owner", b"no")]
            turn = None
            try:
                with patch.object(KiroSDKClient, "connect", connect):
                    turn = asyncio.create_task(request(app, "POST", "/invocations", b'{"prompt":"hello"}', headers,
                                                       timeout=20))
                    for _ in range(200):
                        status, body, _ = await request(app, "POST", "/invocations",
                            b'{"action":"interactions"}', headers)
                        self.assertEqual(status, 200)
                        pending = json.loads(body)["pending"]
                        if pending:
                            break
                        await asyncio.sleep(0.02)
                    else:
                        self.fail("OAuth interaction did not arrive")
                    item = pending[0]
                    self.assertEqual(item["kind"], "mcp_oauth")
                    self.assertEqual(connected, [])
                    self.assertEqual((await request(app, "POST", "/invocations",
                        b'{"action":"interactions"}', other))[0], 403)
                    answer = {"action": "respond", "interaction_id": item["interaction_id"],
                              "redirect_url": await fixture.authorize(item["request"]["authorization_url"])}
                    data = json.dumps(answer).encode()
                    self.assertEqual((await request(app, "POST", "/invocations", data, other))[0], 403)
                    self.assertEqual((await request(app, "POST", "/invocations", data, headers))[0], 200)
                    status, body, _ = await turn
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["response"], "hello")
                    self.assertEqual(connected, [["authorization_code"]])
                    session = app.state.kiro_agentcore.sessions["session-a"]
                    self.assertIsNotNone(await session.client.options.mcp_oauth["probe"].storage.get_tokens())
            finally:
                if turn is not None and not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                await app.state.kiro_agentcore.close()
                await fixture.close()

    async def test_inline_server_is_rejected_before_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), cli_path=str(FAKE), api_key="fixture",
                allow_interactions=True, allow_mcp_oauth_interactions=True, preauthorize_mcp_oauth=True)
            async def owner(req, session_id):
                return True
            def options(workspace, resume):
                return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                    mcp_oauth={"agent:plugin:inline": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
            app = create_agentcore_app(config, options_factory=options, session_authorizer=owner)
            try:
                with patch.object(KiroSDKClient, "connect", side_effect=AssertionError("must not connect")):
                    status, body, _ = await request(app, "POST", "/invocations", b'{"prompt":"hello"}',
                                                    [(HEADER, b"session-a")])
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["is_error"])
            finally:
                await app.state.kiro_agentcore.close()

    async def _file_preauthorization(self, *, change_file, inline_json=False):
        fixture, other_fixture = OAuthFixture(), OAuthFixture()
        url, other_url = await fixture.start(), await other_fixture.start()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mcp.json"
            source.write_text(json.dumps({"mcpServers": {"probe": {"type": "http", "url": url}}}))
            source_value = source.read_text() if inline_json else source
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "state", cli_path=str(FAKE), api_key="fixture",
                allow_interactions=True, allow_mcp_oauth_interactions=True, preauthorize_mcp_oauth=True,
                mcp_oauth_preauthorize_servers={"probe": {"type": "http", "url": url}},
                startup_timeout=10, turn_timeout=10, interaction_timeout=5)
            async def owner(req, session_id):
                return True
            def options(workspace, resume):
                return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all", mcp_servers=source_value,
                    mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
            app = create_agentcore_app(config, options_factory=options, session_authorizer=owner)
            original_connect = KiroSDKClient.connect
            connected = []
            async def connect(client, *args, **kwargs):
                result = await original_connect(client, *args, **kwargs)
                connected.append(True)
                client._mcp_status_tracker.update("_kiro/mcp/status", {
                    "sessionId": client.session_id, "servers": [{"name": "probe", "status": "connected"}]})
                return result

            headers = [(HEADER, b"session-a")]
            turn = None
            try:
                with patch.object(KiroSDKClient, "connect", connect):
                    turn = asyncio.create_task(request(app, "POST", "/invocations", b'{"prompt":"hello"}', headers))
                    for _ in range(200):
                        _, body, _ = await request(app, "POST", "/invocations",
                                                   b'{"action":"interactions"}', headers)
                        pending = json.loads(body)["pending"]
                        if pending:
                            break
                        await asyncio.sleep(0.02)
                    else:
                        self.fail("OAuth interaction did not arrive")
                    item = pending[0]
                    if change_file:
                        changed = json.dumps({"mcpServers": {"probe": {"type": "http", "url": other_url}}})
                        if inline_json:
                            app.state.kiro_agentcore.sessions["session-a"].client.options.mcp_servers = changed
                        else:
                            source.write_text(changed)
                    answer = {"action": "respond", "interaction_id": item["interaction_id"],
                              "redirect_url": await fixture.authorize(item["request"]["authorization_url"])}
                    self.assertEqual((await request(app, "POST", "/invocations",
                                                   json.dumps(answer).encode(), headers))[0], 200)
                    status, body, _ = await turn
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["is_error"], change_file)
                    self.assertEqual(connected, [] if change_file else [True])
                    self.assertEqual(other_fixture.requests, [])
                    self.assertEqual(fixture.token_grants, [] if change_file and inline_json
                                     else ["authorization_code"])
            finally:
                if turn is not None and not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                await app.state.kiro_agentcore.close()
                await fixture.close()
                await other_fixture.close()

    async def test_config_file_preauthorizes_and_reuses_session_credentials(self):
        await self._file_preauthorization(change_file=False)

    async def test_config_file_change_cannot_send_token_to_other_server(self):
        await self._file_preauthorization(change_file=True)

    async def test_json_config_preauthorizes_and_reuses_session_credentials(self):
        await self._file_preauthorization(change_file=False, inline_json=True)

    async def test_json_config_change_cannot_exchange_the_authorization_code(self):
        await self._file_preauthorization(change_file=True, inline_json=True)

    async def test_changed_oauth_config_cannot_exchange_the_authorization_code(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        with tempfile.TemporaryDirectory() as directory:
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), cli_path=str(FAKE), api_key="fixture",
                allow_interactions=True, allow_mcp_oauth_interactions=True, preauthorize_mcp_oauth=True,
                startup_timeout=10, turn_timeout=10, interaction_timeout=5)
            async def owner(req, session_id):
                return True
            def options(workspace, resume):
                return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                    mcp_servers={"probe": {"type": "http", "url": url}},
                    mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
            app = create_agentcore_app(config, options_factory=options, session_authorizer=owner)
            headers = [(HEADER, b"session-a")]
            turn = None
            try:
                with patch.object(KiroSDKClient, "connect", side_effect=AssertionError("must not connect")):
                    turn = asyncio.create_task(request(app, "POST", "/invocations", b'{"prompt":"hello"}', headers))
                    for _ in range(200):
                        _, body, _ = await request(app, "POST", "/invocations",
                                                   b'{"action":"interactions"}', headers)
                        pending = json.loads(body)["pending"]
                        if pending:
                            break
                        await asyncio.sleep(0.02)
                    else:
                        self.fail("OAuth interaction did not arrive")
                    item = pending[0]
                    session = app.state.kiro_agentcore.sessions["session-a"]
                    session.client.options.mcp_oauth["probe"].allowed_origins.append("https://example.com")
                    answer = {"action": "respond", "interaction_id": item["interaction_id"],
                              "redirect_url": await fixture.authorize(item["request"]["authorization_url"])}
                    self.assertEqual((await request(app, "POST", "/invocations",
                                                   json.dumps(answer).encode(), headers))[0], 200)
                    status, body, _ = await turn
                    self.assertEqual(status, 200)
                    self.assertTrue(json.loads(body)["is_error"])
                    self.assertEqual(fixture.token_grants, [])
            finally:
                if turn is not None and not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                await app.state.kiro_agentcore.close()
                await fixture.close()

    async def test_cancelled_preauthorization_clears_pending_interaction(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        with tempfile.TemporaryDirectory() as directory:
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), cli_path=str(FAKE), api_key="fixture",
                allow_interactions=True, allow_mcp_oauth_interactions=True, preauthorize_mcp_oauth=True,
                startup_timeout=10, turn_timeout=10, interaction_timeout=5)
            async def owner(req, session_id):
                return True
            def options(workspace, resume):
                return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                    mcp_servers={"probe": {"type": "http", "url": url}},
                    mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
            app = create_agentcore_app(config, options_factory=options, session_authorizer=owner)
            headers = [(HEADER, b"session-a")]
            turn = None
            try:
                with patch.object(KiroSDKClient, "connect", side_effect=AssertionError("must not connect")):
                    turn = asyncio.create_task(request(app, "POST", "/invocations", b'{"prompt":"hello"}', headers))
                    for _ in range(200):
                        _, body, _ = await request(app, "POST", "/invocations",
                                                   b'{"action":"interactions"}', headers)
                        if json.loads(body)["pending"]:
                            break
                        await asyncio.sleep(0.02)
                    else:
                        self.fail("OAuth interaction did not arrive")
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                    session = app.state.kiro_agentcore.sessions["session-a"]
                    self.assertEqual(session.interactions, {})
                    self.assertEqual(session.oauth_pins, 0)
                    self.assertEqual(fixture.token_grants, [])
            finally:
                if turn is not None and not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                await app.state.kiro_agentcore.close()
                await fixture.close()
