"""Opt-in AgentCore HTTP integration against a locally authenticated Kiro CLI."""
import asyncio
import importlib.util
import base64
import json
import os
import shutil
import struct
import tempfile
import time
import unittest
import uuid
import zlib
from pathlib import Path

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, McpOAuthConfig, delete_session
from test_agentcore import HEADER, request

AVAILABLE = (os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli") and
             importlib.util.find_spec("starlette") is not None)
if AVAILABLE:
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


@unittest.skipUnless(AVAILABLE, "requires authenticated Kiro CLI and the agentcore extra")
class AgentCoreLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_v3_code_intelligence_controls(self):
        with tempfile.TemporaryDirectory(prefix="kiro-agentcore-code-") as root:
            root = Path(root)
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "xdg-state"),
                   "XDG_CONFIG_HOME": str(root / "xdg-config"), "XDG_CACHE_HOME": str(root / "xdg-cache")}

            async def authorizer(req, session_id):
                return req.headers.get("x-test-owner") == "owner"

            def options(workspace, resume):
                (workspace / "sample.py").write_text("def example():\n    return 42\n", encoding="utf-8")
                return KiroAgentOptions(tools=[], enable_kiro_code_intelligence=True, env=env)

            app = create_agentcore_app(
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "state", auth_mode="cli", startup_timeout=90, turn_timeout=90),
                options_factory=options, session_authorizer=authorizer)
            runtime_id = "agentcore-integration-code-0123456789"
            headers = [(HEADER, runtime_id.encode()), (b"x-test-owner", b"owner")]
            session_id = workspace = None
            try:
                status, body, _ = await request(app, "POST", "/invocations",
                    b'{"prompt":"Reply with the single word READY. Do not use tools."}', headers, timeout=100)
                self.assertEqual(status, 200, body)
                self.assertFalse(json.loads(body)["is_error"])
                session = app.state.kiro_agentcore.sessions[runtime_id]
                session_id, workspace = session.client.session_id, session.directory / "workspace"
                denied = [(HEADER, runtime_id.encode()), (b"x-test-owner", b"other")]
                self.assertEqual((await request(app, "POST", "/invocations",
                    b'{"action":"code_status"}', denied))[0], 403)
                for action in ("code_status", "code_init", "code_overview"):
                    status, body, _ = await request(app, "POST", "/invocations",
                        json.dumps({"action": action}).encode(), headers, timeout=100)
                    result = json.loads(body)
                    self.assertEqual(status, 200, body)
                    self.assertFalse(result["is_error"])
                    self.assertTrue(result["result"]["success"])
                self.assertTrue((workspace / ".kiro" / "settings" / "lsp.json").is_file())
            finally:
                await app.state.kiro_agentcore.close()
                if session_id and workspace:
                    await delete_session(session_id, cwd=workspace, env=env)

    async def test_v3_prewarm_moves_kiro_startup_out_of_the_first_prompt(self):
        import time
        with tempfile.TemporaryDirectory(prefix="kiro-agentcore-prewarm-") as root:
            root = Path(root)
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "xdg-state"),
                   "XDG_CONFIG_HOME": str(root / "xdg-config"), "XDG_CACHE_HOME": str(root / "xdg-cache")}

            async def authorizer(req, session_id):
                return req.headers.get("x-test-owner") == "owner"

            def options(workspace, resume):
                return KiroAgentOptions(tools=[], env=env)

            app = create_agentcore_app(
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "state", auth_mode="cli", startup_timeout=90, turn_timeout=90),
                options_factory=options, session_authorizer=authorizer)
            prompt = b'{"prompt":"Reply with the single word READY. Do not use tools."}'
            owned = []

            def headers(runtime_id):
                return [(HEADER, runtime_id.encode()), (b"x-test-owner", b"owner")]

            async def first_prompt(runtime_id):
                started = time.monotonic()
                status, body, _ = await request(app, "POST", "/invocations", prompt, headers(runtime_id), timeout=120)
                self.assertEqual(status, 200, body)
                self.assertFalse(json.loads(body)["is_error"])
                return time.monotonic() - started

            try:
                cold = []
                for index in range(2):
                    runtime_id = f"agentcore-integration-cold-{index}-0123456789"
                    cold.append(await first_prompt(runtime_id))
                    session = app.state.kiro_agentcore.sessions[runtime_id]
                    owned.append((session.client.session_id, session.directory / "workspace"))
                warm = []
                for index in range(2):
                    runtime_id = f"agentcore-integration-warm-{index}-0123456789"
                    denied = [(HEADER, runtime_id.encode()), (b"x-test-owner", b"other")]
                    self.assertEqual((await request(app, "POST", "/invocations", b'{"action":"prewarm"}', denied))[0],
                                     403)
                    status, body, _ = await request(app, "POST", "/invocations", b'{"action":"prewarm"}',
                                                    headers(runtime_id), timeout=120)
                    self.assertEqual((status, json.loads(body)["status"]), (200, "ready"), body)
                    session = app.state.kiro_agentcore.sessions[runtime_id]
                    prewarmed = session.client.session_id
                    warm.append(await first_prompt(runtime_id))
                    self.assertEqual(session.client.session_id, prewarmed)
                    owned.append((prewarmed, session.directory / "workspace"))
                # The prewarmed first prompt skips Kiro startup, so even the best cold run is slower.
                self.assertLess(min(warm), min(cold), {"cold": cold, "warm": warm})
            finally:
                await app.state.kiro_agentcore.close()
                for session_id, workspace in owned:
                    if session_id:
                        await delete_session(session_id, cwd=workspace, env=env)

    async def test_v3_mcp_oauth_remote_interaction(self):
        if importlib.util.find_spec("mcp") is None:
            self.skipTest("optional mcp dependency not installed")
        from test_mcp_oauth import OAuthFixture
        fixture = OAuthFixture()
        url = await fixture.start()
        async def gate(event, tool_id, context):
            name = event.get("tool_name", "")
            data = event.get("tool_input") or {}
            if ((name == "tool_load" and data.get("tool_ids") == ["oauth_probe::probe"]) or
                    ("oauth_probe" in name and "probe" in name)):
                return {}
            return {'block': "Only the isolated OAuth test tool is allowed"}
        async def authorizer(req, session_id):
            return req.headers.get("x-test-owner") == "owner"
        def options(workspace, resume):
            return KiroAgentOptions(tools=["@mcp"], sdk_mcp_toggle_mode="sdk-all",
                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                mcp_servers={"oauth_probe": {"type": "http", "url": url}},
                mcp_oauth={"oauth_probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
        with tempfile.TemporaryDirectory() as root:
            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(root), auth_mode="cli", turn_timeout=100,
                allow_interactions=True, allow_mcp_oauth_interactions=True, interaction_timeout=60),
                options_factory=options, session_authorizer=authorizer)
            runtime_id = "agentcore-integration-oauth-0123456789"
            headers = [(HEADER, runtime_id.encode()), (b"x-test-owner", b"owner")]
            payload = {"prompt": "Call the oauth_probe probe tool once and report its result. Do not use other tools."}
            turn = asyncio.create_task(request(app, "POST", "/invocations", json.dumps(payload).encode(),
                                               headers, timeout=110))
            session_id = workspace = None
            seen = 0
            try:
                deadline = time.monotonic() + 95
                while not turn.done() and time.monotonic() < deadline:
                    session = app.state.kiro_agentcore.sessions.get(runtime_id)
                    if session and session.client and session.client.session_id:
                        session_id, workspace = session.client.session_id, session.directory / "workspace"
                    status, body, _ = await request(app, "POST", "/invocations",
                                                    b'{"action":"interactions"}', headers, timeout=10)
                    self.assertEqual(status, 200)
                    for item in json.loads(body)["pending"]:
                        response = {"action": "respond", "interaction_id": item["interaction_id"]}
                        if item["kind"] == "mcp_oauth":
                            response["redirect_url"] = await fixture.authorize(item["request"]["authorization_url"])
                            seen += 1
                        elif item["kind"] == "permission":
                            details = json.dumps(item["request"])
                            response["decision"] = "allow" if "oauth_probe" in details else "deny"
                        else:
                            self.fail("Unexpected interaction")
                        status, _, _ = await request(app, "POST", "/invocations",
                                                     json.dumps(response).encode(), headers, timeout=10)
                        self.assertEqual(status, 200)
                    await asyncio.sleep(0.1)
                self.assertTrue(turn.done())
                status, body, _ = await turn
                self.assertEqual(status, 200)
                self.assertEqual(seen, 1)
                self.assertEqual(fixture.calls, ["probe"])
                self.assertIn("OAUTH_PROBE_OK", json.loads(body)["response"].upper())
                reconnect_payload = {"action": "mcp_reconnect", "server_name": "oauth_probe", "start_oauth": True}
                reconnect = asyncio.create_task(request(app, "POST", "/invocations",
                    json.dumps(reconnect_payload).encode(), headers, timeout=100))
                try:
                    deadline = time.monotonic() + 80
                    while len(fixture.token_grants) < 2 and time.monotonic() < deadline:
                        status, body, _ = await request(app, "POST", "/invocations",
                                                        b'{"action":"interactions"}', headers, timeout=10)
                        self.assertEqual(status, 200)
                        for item in json.loads(body)["pending"]:
                            if item["kind"] == "mcp_oauth":
                                answer = {"action": "respond", "interaction_id": item["interaction_id"],
                                          "redirect_url": await fixture.authorize(item["request"]["authorization_url"])}
                                status, _, _ = await request(app, "POST", "/invocations",
                                                             json.dumps(answer).encode(), headers, timeout=10)
                                self.assertEqual(status, 200)
                        await asyncio.sleep(0.1)
                    self.assertEqual(fixture.token_grants, ["authorization_code", "authorization_code"])
                    self.assertEqual((await reconnect)[0], 200)
                finally:
                    if not reconnect.done():
                        reconnect.cancel()
                        await asyncio.gather(reconnect, return_exceptions=True)
            finally:
                if not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                await app.state.kiro_agentcore.close()
                if session_id and workspace:
                    await delete_session(session_id, cwd=workspace)
                await fixture.close()

    async def test_v3_mcp_oauth_preauthorization_before_kiro(self):
        await self._mcp_oauth_preauthorization_before_kiro(use_config_file=False)

    async def test_v3_file_mcp_oauth_preauthorization_before_kiro(self):
        await self._mcp_oauth_preauthorization_before_kiro(use_config_file=True)

    async def _mcp_oauth_preauthorization_before_kiro(self, *, use_config_file):
        if importlib.util.find_spec("mcp") is None:
            self.skipTest("optional mcp dependency not installed")
        from test_mcp_oauth import OAuthFixture
        fixture = OAuthFixture()
        url = await fixture.start()

        async def gate(event, tool_id, context):
            name, data = event.get("tool_name", ""), event.get("tool_input") or {}
            allowed = ((name == "tool_load" and data.get("tool_ids") == ["oauth_probe::probe"]) or
                       ("oauth_probe" in name and "probe" in name))
            return {} if allowed else {"block": "Only the test tool is allowed"}

        async def owner(req, session_id):
            return req.headers.get("x-test-owner") == "owner"

        def options(workspace, resume):
            return KiroAgentOptions(tools=["@mcp"], sdk_mcp_toggle_mode="sdk-all",
                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                mcp_servers=source if use_config_file else {"oauth_probe": {"type": "http", "url": url}},
                mcp_oauth={"oauth_probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "mcp.json"
            if use_config_file:
                source.write_text(json.dumps({"mcpServers": {"oauth_probe": {"type": "http", "url": url}}}))
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(root), auth_mode="cli", turn_timeout=100,
                allow_interactions=True, allow_mcp_oauth_interactions=True, preauthorize_mcp_oauth=True,
                mcp_oauth_preauthorize_servers=(
                    {"oauth_probe": {"type": "http", "url": url}} if use_config_file else {}),
                interaction_timeout=75)
            app = create_agentcore_app(config, options_factory=options, session_authorizer=owner)
            runtime_id = "agentcore-integration-preauthorize-012345"
            headers = [(HEADER, runtime_id.encode()), (b"x-test-owner", b"owner")]
            prompt = {"prompt": "Call the oauth_probe probe tool once and report its result. Do not use other tools."}
            turn = asyncio.create_task(request(app, "POST", "/invocations", json.dumps(prompt).encode(),
                                               headers, timeout=160))
            try:
                for _ in range(300):
                    status, body, _ = await request(app, "POST", "/invocations",
                                                    b'{"action":"interactions"}', headers)
                    self.assertEqual(status, 200)
                    pending = json.loads(body)["pending"]
                    if pending:
                        break
                    await asyncio.sleep(0.1)
                else:
                    self.fail("OAuth interaction did not arrive")
                item = pending[0]
                self.assertEqual(item["kind"], "mcp_oauth")
                session = app.state.kiro_agentcore.sessions[runtime_id]
                self.assertFalse(session.client._connected)
                await asyncio.sleep(35)
                self.assertFalse(session.client._connected)
                answer = {"action": "respond", "interaction_id": item["interaction_id"],
                          "redirect_url": await fixture.authorize(item["request"]["authorization_url"])}
                self.assertEqual((await request(app, "POST", "/invocations", json.dumps(answer).encode(),
                                                headers))[0], 200)
                deadline = time.monotonic() + 100
                while not turn.done() and time.monotonic() < deadline:
                    status, body, _ = await request(app, "POST", "/invocations",
                                                    b'{"action":"interactions"}', headers)
                    self.assertEqual(status, 200)
                    for interaction in json.loads(body)["pending"]:
                        self.assertEqual(interaction["kind"], "permission")
                        details = json.dumps(interaction["request"])
                        response = {"action": "respond", "interaction_id": interaction["interaction_id"],
                                    "decision": "allow" if "oauth_probe" in details else "deny"}
                        self.assertEqual((await request(app, "POST", "/invocations",
                            json.dumps(response).encode(), headers))[0], 200)
                    await asyncio.sleep(0.1)
                self.assertTrue(turn.done())
                status, body, _ = await turn
                self.assertEqual(status, 200)
                self.assertIn("OAUTH_PROBE_OK", json.loads(body)["response"].upper())
                self.assertEqual(fixture.token_grants, ["authorization_code"])
                self.assertEqual(fixture.calls, ["probe"])
            finally:
                if not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                session = app.state.kiro_agentcore.sessions.get(runtime_id)
                session_id = session.client.session_id if session and session.client else None
                workspace = session.directory / "workspace" if session else None
                await app.state.kiro_agentcore.close()
                if session_id and workspace:
                    await delete_session(session_id, cwd=workspace)
                await fixture.close()

    async def test_v3_http_remote_mcp_elicitation(self):
        try:
            import mcp.types as types
            from mcp.server import Server
        except ImportError:
            self.skipTest("optional mcp dependency not installed")
        if hasattr(types, "jsonrpc_message_adapter"):
            self.skipTest("this MCP protocol does not allow server-initiated elicitation")
        suffix = uuid.uuid4().hex[:8]
        server_name, tool_name = "sdk_http_" + suffix, "sdk_color_" + suffix
        server = Server(server_name)

        @server.list_tools()
        async def list_tools():
            return [types.Tool(name=tool_name, description="Ask the user for a color.",
                               inputSchema={"type": "object", "properties": {}})]

        @server.call_tool()
        async def call_tool(name, arguments):
            response = await server.request_context.session.elicit(
                "Choose a color", {"type": "object", "properties": {"color": {"type": "string"}},
                                   "required": ["color"]})
            return [types.TextContent(type="text", text=str(response.content or response.action))]

        async def gate(event, tool_id, context):
            text = event.get("tool_name", "") + json.dumps(event.get("tool_input") or {})
            return {} if server_name in text or tool_name in text else {
                "decision": "block", "reason": "Only the isolated test tool is allowed"}

        def options(workspace, resume):
            return KiroAgentOptions(tools=["@mcp"], native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                mcp_servers={server_name: {"type": "sdk", "name": server_name, "instance": server}})

        with tempfile.TemporaryDirectory() as root:
            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(root), auth_mode="cli", turn_timeout=100,
                                                       allow_interactions=True, interaction_timeout=60),
                                       options_factory=options)
            header = [(HEADER, b"agentcore-integration-mcp-input-0123456789")]
            runtime_id = header[0][1].decode()
            prompt = json.dumps({"prompt": f"Call the {tool_name} tool, wait for its response, and report the color. "
                                          "Do not use other tools."}).encode()
            turn = asyncio.create_task(request(app, "POST", "/invocations", prompt, header, timeout=110))
            session_id = workspace = None
            seen = 0
            try:
                deadline = time.monotonic() + 95
                while not turn.done() and time.monotonic() < deadline:
                    session = app.state.kiro_agentcore.sessions.get(runtime_id)
                    if session and session.client and session.client.session_id:
                        session_id = session.client.session_id
                        workspace = session.directory / "workspace"
                    status, body, _ = await request(app, "POST", "/invocations",
                                                    b'{"action":"interactions"}', header, timeout=10)
                    self.assertEqual(status, 200, body)
                    for item in json.loads(body)["pending"]:
                        response = {"action": "respond", "interaction_id": item["interaction_id"]}
                        if item["kind"] == "permission":
                            text = json.dumps(item["request"], ensure_ascii=False)
                            response["decision"] = "allow" if server_name in text or tool_name in text else "deny"
                        elif item["kind"] == "mcp_elicitation":
                            response["response"] = {"action": "accept", "content": {"color": "PURPLE"}}
                            seen += 1
                        else:
                            response["answer"] = None
                        status, body, _ = await request(app, "POST", "/invocations",
                                                        json.dumps(response).encode(), header, timeout=10)
                        self.assertEqual(status, 200, body)
                    await asyncio.sleep(0.2)
                self.assertTrue(turn.done(), "Kiro did not complete the MCP test")
                status, body, _ = await turn
                self.assertEqual(status, 200, body)
                self.assertGreater(seen, 0)
                self.assertIn("PURPLE", json.loads(body)["response"].upper())
            finally:
                if not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                await app.state.kiro_agentcore.close()
                if session_id and workspace:
                    await delete_session(session_id, cwd=workspace)

    async def test_v3_http_remote_permission_denial(self):
        with tempfile.TemporaryDirectory() as root:
            session_env = {}
            async def gate(event, tool_id, context):
                name = event.get("tool_name", "").lower()
                data = json.dumps(event.get("tool_input") or {}).lower()
                permitted = ((name == "tool_load" and "write" in data) or
                             ("probe.txt" in data and "write" in name))
                return {} if permitted else {"block": "Only the test file is allowed"}

            def options(workspace, resume):
                session_env.update({"KIRO_HOME": str(workspace.parent / "kiro"),
                                    "XDG_STATE_HOME": str(workspace.parent / "state"),
                                    "XDG_CONFIG_HOME": str(workspace.parent / "config"),
                                    "XDG_CACHE_HOME": str(workspace.parent / "cache")})
                return KiroAgentOptions(tools=["write"], env=session_env,
                    kiro_permissions={"rules": [{"capability": "fs_write", "effect": "ask"}]},
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]})

            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(root), auth_mode="cli", turn_timeout=90,
                                                       allow_interactions=True, interaction_timeout=45),
                                       options_factory=options)
            header = [(HEADER, b"agentcore-integration-permission-0123456789")]
            runtime_id = header[0][1].decode()
            turn = asyncio.create_task(request(app, "POST", "/invocations",
                b'{"prompt":"Write TEST to probe.txt. Do not use other files or tools."}', header, timeout=100))
            session_id = workspace = None
            seen = 0
            try:
                deadline = time.monotonic() + 80
                while not turn.done() and time.monotonic() < deadline:
                    session = app.state.kiro_agentcore.sessions.get(runtime_id)
                    if session and session.client and session.client.session_id:
                        session_id = session.client.session_id
                        workspace = session.directory / "workspace"
                    status, body, _ = await request(app, "POST", "/invocations",
                                                    b'{"action":"interactions"}', header, timeout=10)
                    self.assertEqual(status, 200, body)
                    for item in json.loads(body)["pending"]:
                        self.assertEqual(item["kind"], "permission")
                        response = json.dumps({"action": "respond", "interaction_id": item["interaction_id"],
                                               "decision": "deny"}).encode()
                        status, body, _ = await request(app, "POST", "/invocations", response, header, timeout=10)
                        self.assertEqual(status, 200, body)
                        seen += 1
                    await asyncio.sleep(0.2)
                self.assertTrue(turn.done(), "Kiro did not complete the permission test")
                status, body, _ = await turn
                self.assertEqual(status, 200, body)
                self.assertGreater(seen, 0)
                self.assertFalse((workspace / "probe.txt").exists())
            finally:
                if not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                await app.state.kiro_agentcore.close()
                if session_id and workspace:
                    await delete_session(session_id, cwd=workspace, env=session_env)

    async def test_v3_http_owner_revocation_while_permission_is_pending_blocks_the_effect(self):
        with tempfile.TemporaryDirectory() as root:
            session_env, owner = {}, {"allowed": True}

            async def authorizer(req, session_id):
                return owner["allowed"] and req.headers.get("x-test-owner") == "owner"

            def options(workspace, resume):
                session_env.update({"KIRO_HOME": str(workspace.parent / "kiro"),
                                    "XDG_STATE_HOME": str(workspace.parent / "state"),
                                    "XDG_CONFIG_HOME": str(workspace.parent / "config"),
                                    "XDG_CACHE_HOME": str(workspace.parent / "cache")})
                return KiroAgentOptions(tools=["write"], env=session_env,
                    kiro_permissions={"rules": [{"capability": "fs_write", "effect": "ask"}]})

            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(root), auth_mode="cli", turn_timeout=90,
                                                       allow_interactions=True, interaction_timeout=20),
                                       options_factory=options, session_authorizer=authorizer)
            runtime_id = "agentcore-integration-revoke-0123456789"
            header = [(HEADER, runtime_id.encode()), (b"x-test-owner", b"owner")]
            turn = asyncio.create_task(request(app, "POST", "/invocations",
                b'{"prompt":"Write TEST to probe.txt with the write tool. Use no other tools."}', header, timeout=100))
            session_id = workspace = pending = None
            try:
                deadline = time.monotonic() + 80
                while not turn.done() and time.monotonic() < deadline and pending is None:
                    session = app.state.kiro_agentcore.sessions.get(runtime_id)
                    if session and session.client and session.client.session_id:
                        session_id, workspace = session.client.session_id, session.directory / "workspace"
                    status, body, _ = await request(app, "POST", "/invocations",
                                                    b'{"action":"interactions"}', header, timeout=10)
                    self.assertEqual(status, 200, body)
                    items = json.loads(body)["pending"]
                    pending = items[0]["interaction_id"] if items else None
                    await asyncio.sleep(0.2)
                if pending is None:
                    self.skipTest("model did not request the write permission")
                owner["allowed"] = False
                response = json.dumps({"action": "respond", "interaction_id": pending, "decision": "allow"}).encode()
                status, body, _ = await request(app, "POST", "/invocations", response, header, timeout=10)
                self.assertEqual(status, 403, body)
                await asyncio.wait_for(turn, 100)
                self.assertFalse((workspace / "probe.txt").exists())
                status, _, _ = await request(app, "POST", "/invocations", b'{"action":"interactions"}', header)
                self.assertEqual(status, 403)
            finally:
                if not turn.done():
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)
                await app.state.kiro_agentcore.close()
                if session_id and workspace:
                    await delete_session(session_id, cwd=workspace, env=session_env)

    async def test_v3_http_steer_and_interrupt_active_turns(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(root), auth_mode="cli", turn_timeout=90))
            header = [(HEADER, b"agentcore-integration-control-0123456789")]
            runtime_id = header[0][1].decode()
            outstanding = None
            session_id = None
            workspace = None

            async def wait_ready():
                async def ready():
                    while True:
                        session = app.state.kiro_agentcore.sessions.get(runtime_id)
                        if session and session.turn_ready and session.client._turn and not session.client._turn.done():
                            return session
                        await asyncio.sleep(0.01)
                return await asyncio.wait_for(ready(), 30)

            try:
                prompt = json.dumps({"prompt": "Explain the history of space exploration in several paragraphs."}).encode()
                outstanding = asyncio.create_task(request(app, "POST", "/invocations", prompt, header, timeout=100))
                session = await wait_ready()
                session_id = session.client.session_id
                workspace = session.directory / "workspace"
                control = json.dumps({"action": "steer", "message": "Focus on lunar missions."}).encode()
                status, body, _ = await request(app, "POST", "/invocations", control, header, timeout=30)
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["status"], "requested")
                status, body, _ = await outstanding
                outstanding = None
                self.assertEqual(status, 200, body)
                self.assertFalse(json.loads(body)["is_error"])

                outstanding = asyncio.create_task(request(app, "POST", "/invocations", prompt, header, timeout=100))
                await wait_ready()
                status, body, _ = await request(app, "POST", "/invocations",
                                                b'{"action":"interrupt"}', header, timeout=30)
                self.assertEqual(status, 200, body)
                status, body, _ = await outstanding
                outstanding = None
                self.assertEqual(status, 200, body)
                self.assertTrue(json.loads(body)["is_error"])
            finally:
                if outstanding is not None:
                    outstanding.cancel()
                    await asyncio.gather(outstanding, return_exceptions=True)
                await app.state.kiro_agentcore.close()
                if session_id and workspace:
                    await delete_session(session_id, cwd=workspace)

    async def test_v3_http_image_and_document_input(self):
        def chunk(name, data):
            return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
        pixels = (b"\x00" + b"\xff\x00\x00" * 64) * 64
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)) +
               chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
        content = b"BT /F1 24 Tf 50 700 Td (Marker: COCONUT927) Tj ET"
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
                   b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                   b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                   b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
        pdf, offsets = b"%PDF-1.4\n", []
        for index, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
        xref = len(pdf)
        pdf += b"xref\n0 6\n0000000000 65535 f \n"
        pdf += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
        pdf += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
        with tempfile.TemporaryDirectory() as root:
            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(root), auth_mode="cli", turn_timeout=90))
            header = [(HEADER, b"agentcore-integration-attachments-0123456789")]
            session_id = None
            workspace = None
            try:
                blocks = [{"type": "text", "text": "Report the image color and both attached document markers."},
                          {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()},
                          {"type": "resource", "resource": {"uri": "memory://marker.pdf", "mimeType": "application/pdf",
                                                            "blob": base64.b64encode(pdf).decode()}},
                          {"type": "resource", "resource": {"uri": "memory://marker.txt", "mimeType": "text/plain",
                                                            "text": "Marker: PERSIMMON793"}}]
                status, body, _ = await request(app, "POST", "/invocations",
                                                json.dumps({"prompt": blocks}).encode(), header, timeout=100)
                self.assertEqual(status, 200, body)
                result = json.loads(body)
                session_id = result["kiro_session_id"]
                workspace = app.state.kiro_agentcore.sessions[header[0][1].decode()].directory / "workspace"
                self.assertIn("RED", result["response"].upper())
                self.assertIn("COCONUT927", result["response"].upper())
                self.assertIn("PERSIMMON793", result["response"].upper())
            finally:
                await app.state.kiro_agentcore.close()
                if session_id and workspace:
                    await delete_session(session_id, cwd=workspace)

    async def test_v3_http_invocation_eviction_and_resume_after_server_restart(self):
        with tempfile.TemporaryDirectory() as root:
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(root), auth_mode="cli", turn_timeout=90,
                                     allow_sdk_events=True, max_sessions=1)
            first = create_agentcore_app(config)
            second = None
            sessions = []
            try:
                header = [(HEADER, b"agentcore-integration-session-0123456789")]
                prompt = json.dumps({"prompt": "Remember the code BRONZE572. Reply with READY."}).encode()
                status, body, _ = await request(first, "POST", "/invocations", prompt, header, timeout=100)
                self.assertEqual(status, 200, body)
                result = json.loads(body)
                self.assertIn("READY", result["response"].upper())
                session_id = result["kiro_session_id"]
                workspace = first.state.kiro_agentcore.sessions[header[0][1].decode()].directory / "workspace"
                sessions.append((session_id, workspace))
                other_header = [(HEADER, b"agentcore-integration-session-other-012345")]
                other_prompt = json.dumps({"prompt": "Reply with READY only."}).encode()
                status, body, _ = await request(first, "POST", "/invocations", other_prompt, other_header, timeout=100)
                self.assertEqual(status, 200, body)
                other_workspace = first.state.kiro_agentcore.sessions[other_header[0][1].decode()].directory / "workspace"
                sessions.append((json.loads(body)["kiro_session_id"], other_workspace))
                prompt = json.dumps({"prompt": "What code did I ask you to remember? Reply with the code."}).encode()
                status, body, _ = await request(first, "POST", "/invocations", prompt, header, timeout=100)
                self.assertEqual(status, 200, body)
                self.assertIn("BRONZE572", json.loads(body)["response"].upper())
                self.assertEqual(json.loads(body)["kiro_session_id"], session_id)
                await first.state.kiro_agentcore.close()
                second = create_agentcore_app(config)
                prompt = json.dumps({"prompt": "What code did I ask you to remember? Reply with the code."}).encode()
                status, body, _ = await request(second, "POST", "/invocations", prompt, header, timeout=100)
                self.assertEqual(status, 200, body)
                self.assertIn("BRONZE572", json.loads(body)["response"].upper())
                self.assertEqual(json.loads(body)["kiro_session_id"], session_id)
                prompt = json.dumps({"prompt": "Reply with SILVER only.", "stream": True,
                                     "include_events": True}).encode()
                status, body, _ = await request(second, "POST", "/invocations", prompt, header, timeout=100)
                self.assertEqual(status, 200, body)
                events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b"data: ")]
                names = {item["event_type"] for item in events if item["type"] == "sdk_event"}
                self.assertTrue({"StreamEvent", "AgentMessage", "ResultMessage"} <= names)
                self.assertIn("SILVER", events[-1]["response"].upper())
            finally:
                await first.state.kiro_agentcore.close()
                if second:
                    await second.state.kiro_agentcore.close()
                for session_id, workspace in sessions:
                    await delete_session(session_id, cwd=workspace)
