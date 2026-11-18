import asyncio
import importlib.util
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HAS_STARLETTE = importlib.util.find_spec("starlette") is not None
if HAS_STARLETTE:
    from kiro_agent_sdk import (AgentDefinition, HookMatcher,
                                KiroAgentOptions, KiroSDKClient, McpOAuthConfig,
                                PermissionResultAllow, create_mcp_server, tool)
    from kiro_agent_sdk.agentcore import (AgentCoreConfig, _copy_runtime_cache, _load_secret,
                                          create_agentcore_app)

FAKE = Path(__file__).with_name("fake_kiro.py").resolve()
HEADER = b"x-amzn-bedrock-agentcore-runtime-session-id"


async def request(app, method, path, body=b"", headers=(), timeout=15):
    incoming = [{"type": "http.request", "body": body, "more_body": False}]
    forever = asyncio.Event()
    sent = []

    async def receive():
        if incoming:
            return incoming.pop(0)
        await forever.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": list(headers),
             "server": ("localhost", 8080), "client": ("localhost", 1)}
    await asyncio.wait_for(app(scope, receive, send), timeout)
    start = next(message for message in sent if message["type"] == "http.response.start")
    content = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return start["status"], content, dict(start.get("headers", []))


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreConstructionTests(unittest.TestCase):
    def test_app_created_before_server_loop_uses_the_running_loop(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(root), api_key="synthetic"))
            service = app.state.kiro_agentcore
            self.assertIsNone(service.pool_lock)

            async def exercise():
                first, error = await service._lease("first")
                self.assertIsNone(error)
                await first.release()
                await service.pool_lock.acquire()
                pending = asyncio.create_task(service._lease("second"))
                await asyncio.sleep(0)
                service.pool_lock.release()
                second, error = await pending
                self.assertIsNone(error)
                await second.release()
                await service.close()
            asyncio.run(exercise())


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", cli_path=str(FAKE),
                                      api_key="synthetic-test-key", startup_timeout=10, turn_timeout=10)
        self.apps = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    def app(self, config=None, factory=None):
        if factory is None:
            factory = lambda workspace, resume: KiroAgentOptions(
                tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)})
        app = create_agentcore_app(config or self.config, options_factory=factory)
        self.apps.append(app)
        return app

    async def test_configuration_rejects_ambiguous_auth_and_hides_key(self):
        self.assertNotIn("synthetic-test-key", repr(self.config))
        for kwargs in ({}, {"api_key": "x", "secret_id": "y"}, {"auth_mode": "cli", "api_key": "x"},
                       {"api_key": "x", "state_dir": None}, {"api_key": "x", "turn_timeout": float("inf")},
                       {"api_key": "x", "allow_interactions": "true"},
                       {"api_key": "x", "interaction_timeout": 0},
                       {"api_key": "x", "session_authorization_timeout": 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    AgentCoreConfig(allow_unauthorized_sessions=True, **kwargs)
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=str(self.root), api_key="x")
        self.assertIsInstance(config.state_dir, Path)
        with patch.dict(os.environ, {"KIRO_API_KEY": "x", "KIRO_AGENTCORE_ALLOW_SDK_EVENTS": "true"}, clear=True):
            self.assertTrue(AgentCoreConfig.from_env().allow_sdk_events)
        with patch.dict(os.environ, {"KIRO_API_KEY": "x", "KIRO_AGENTCORE_ALLOW_SDK_EVENTS": "1"}, clear=True):
            with self.assertRaises(ValueError):
                AgentCoreConfig.from_env()
        with patch.dict(os.environ, {"KIRO_API_KEY": "x", "KIRO_AGENTCORE_ALLOW_INTERACTIONS": "true",
                                    "KIRO_AGENTCORE_INTERACTION_TIMEOUT": "5"}, clear=True):
            config = AgentCoreConfig.from_env()
            self.assertTrue(config.allow_interactions)
            self.assertEqual(config.interaction_timeout, 5)
        with patch.dict(os.environ, {"KIRO_API_KEY": "x",
                                    "KIRO_AGENTCORE_SESSION_AUTHORIZATION_TIMEOUT": "2"}, clear=True):
            self.assertEqual(AgentCoreConfig.from_env().session_authorization_timeout, 2)

    async def test_serving_without_session_authorizer_is_an_explicit_choice(self):
        config = AgentCoreConfig(state_dir=self.root / "strict", cli_path=str(FAKE), api_key="x")
        self.assertFalse(config.allow_unauthorized_sessions)
        with self.assertRaisesRegex(ValueError, "session_authorizer"):
            create_agentcore_app(config)
        async def authorize(request, session_id):
            return True
        self.apps.append(create_agentcore_app(config, session_authorizer=authorize))
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
            self.apps.append(create_agentcore_app(
                AgentCoreConfig(state_dir=self.root / "open", cli_path=str(FAKE), api_key="x",
                                allow_unauthorized_sessions=True)))
        with self.assertRaises(ValueError):
            AgentCoreConfig(api_key="x", allow_unauthorized_sessions="true")
        with patch.dict(os.environ, {"KIRO_API_KEY": "x"}, clear=True):
            self.assertFalse(AgentCoreConfig.from_env().allow_unauthorized_sessions)
        with patch.dict(os.environ, {"KIRO_API_KEY": "x", "KIRO_AGENTCORE_ALLOW_UNAUTHORIZED_SESSIONS": "true"},
                        clear=True):
            self.assertTrue(AgentCoreConfig.from_env().allow_unauthorized_sessions)

    async def test_session_authorizer_guards_prompt_and_all_controls(self):
        calls = []

        async def authorizer(req, session_id):
            calls.append(session_id)
            return req.headers.get("x-test-identity") == "owner" and session_id == "session-a"

        app = create_agentcore_app(self.config, session_authorizer=authorizer,
            options_factory=lambda workspace, resume: KiroAgentOptions(
                tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)}))
        self.apps.append(app)
        unauthorized = [(b"x-test-identity", b"other")]
        for payload in ({"prompt": "x"}, {"prompt": "x", "stream": True},
                        {"action": "interrupt"}, {"action": "steer", "message": "x"},
                        {"action": "mcp_cancel_tools", "server_name": "example"},
                        {"action": "mcp_reconnect", "server_name": "example", "start_oauth": True},
                        {"action": "mcp_revoke_oauth", "server_name": "example"},
                        {"action": "code_status"}, {"action": "code_init"}, {"action": "code_overview"},
                        {"action": "list_kiro_hooks"}, {"action": "available_commands"},
                        {"action": "portable_commands"},
                        {"action": "server_info"}, {"action": "context_usage"}, {"action": "mcp_status"},
                        {"action": "sdk_permission_mode"}, {"action": "set_sdk_permission_mode", "mode": "default"},
                        {"action": "sdk_session_denied_tools"},
                        {"action": "sdk_session_denied_resources"},
                        {"action": "set_sdk_session_deny_rules", "operation": "add", "tool_names": ["write"]},
                        {"action": "apply_sdk_session_deny_rules", "updates": []},
                        {"action": "sdk_session_ask_tools"}, {"action": "sdk_session_ask_resources"},
                        {"action": "set_sdk_session_ask_rules", "operation": "add", "tool_names": ["write"]},
                        {"action": "apply_sdk_session_ask_rules", "updates": []},
                        {"action": "apply_sdk_session_permission_rules", "updates": []},
                        {"action": "sdk_session_additional_directories"},
                        {"action": "set_sdk_session_additional_directories", "operation": "add", "directories": ["/tmp"]},
                        {"action": "apply_sdk_session_additional_directories", "updates": []},
                        {"action": "apply_sdk_session_permission_updates", "updates": []},
                        {"action": "sdk_session_allow_rules"},
                        {"action": "set_sdk_session_allow_rules", "operation": "add", "rules": []},
                        {"action": "apply_sdk_session_allow_rules", "updates": []},
                        {"action": "apply_sdk_session_all_permission_updates", "updates": []},
                        {"action": "skill_fork_sessions"},
                        {"action": "forget_skill_fork_session", "session_id": "child-session"},
                        {"action": "interactions"}, {"action": "respond"},
                        {"action": "auth_start"}, {"action": "auth_status"},
                        {"action": "auth_cancel"}, {"action": "auth_logout"}):
            with self.subTest(payload=payload):
                status, body, _ = await self.invoke(app, payload, headers=unauthorized)
                self.assertEqual((status, json.loads(body)), (403, {"error": "Session access denied"}))
        self.assertFalse(app.state.kiro_agentcore.sessions)
        self.assertFalse(self.log.exists())
        status, body, _ = await self.invoke(app, {"prompt": "accepted"},
                                            headers=[(b"x-test-identity", b"owner")])
        self.assertEqual((status, json.loads(body)["response"]), (200, "accepted"))
        self.assertEqual((await self.invoke(app, {"prompt": "denied"}, b"session-b",
                                            [(b"x-test-identity", b"owner")]))[0], 403)
        count = len(calls)
        self.assertEqual((await self.invoke(app, {"prompt": "x"}, b"../invalid"))[0], 400)
        self.assertEqual(len(calls), count)
        self.assertEqual((await request(app, "GET", "/ping"))[0], 200)

    async def test_prompt_hook_block_hides_details_and_accepts_later_prompt(self):
        state = {"block": True}
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        async def hook(event, tool_id, context):
            return {"decision": "block", "reason": "PRIVATE_REASON", "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "suppressOriginalPrompt": True}} if state["block"] else {}
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)},
                hooks={"promptSubmit": [HookMatcher(hooks=[hook])]})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        self.assertEqual((await self.invoke(app, {"prompt": "PRIVATE_PROMPT"},
                                            headers=[(b"x-test-identity", b"other")]))[0], 403)
        for stream in (False, True):
            status, body, _ = await self.invoke(app, {"prompt": "PRIVATE_PROMPT", "stream": stream}, headers=owner)
            self.assertEqual(status, 200)
            self.assertIn("Agent invocation failed", body.decode())
            self.assertNotIn("PRIVATE_", body.decode())
        self.assertNotIn("PRIVATE_PROMPT", self.log.read_text())
        state["block"] = False
        status, body, _ = await self.invoke(app, {"prompt": "allowed"}, headers=owner)
        self.assertEqual((status, json.loads(body)["response"]), (200, "allowed"))

    async def test_prompt_hook_warning_requires_authorized_sdk_event_stream(self):
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        async def hook(event, tool_id, context):
            return {'warning': "PRIVATE_WARNING"}
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "warnings", cli_path=str(FAKE),
                                 api_key="synthetic", allow_sdk_events=True)
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)},
                hooks={"promptSubmit": [HookMatcher(hooks=[hook])]})
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        payload = {"prompt": "hello", "stream": True, "include_events": True}
        self.assertEqual((await self.invoke(app, payload, headers=[(b"x-test-identity", b"other")]))[0], 403)
        status, body, _ = await self.invoke(app, {"prompt": "hello"}, headers=owner)
        self.assertEqual(status, 200)
        self.assertNotIn(b"PRIVATE_WARNING", body)
        status, body, _ = await self.invoke(app, payload, headers=owner)
        self.assertEqual(status, 200)
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b"data: ")]
        warning = next(event for event in events if event.get("event_type") == "HookEventMessage"
                       and event["message"]["kind"] == "hook_warning")
        self.assertEqual(warning["message"]["data"]["message"], "PRIVATE_WARNING")
        self.assertEqual(events[-1]["response"], "hello")
        self.assertNotIn("PRIVATE_WARNING", self.log.read_text())

    @unittest.skipUnless(os.name == "posix", "native Kiro hooks require POSIX")
    async def test_native_hook_warning_callback_in_authorized_session(self):
        from test_sdk_native_pretool import invoke_native
        seen = []
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        async def hook(event, tool_id, context):
            return {'block': "policy", 'warning': "PRIVATE_WARNING"}
        async def notice(event):
            seen.append(event)
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)},
                native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native", on_native_hook_system_message=notice,
                hooks={"preToolUse": [HookMatcher(hooks=[hook])]})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "hello"},
                                            headers=[(b"x-test-identity", b"other")]))[0], 403)
        self.assertEqual((await self.invoke(app, {"prompt": "hello"},
                                            headers=[(b"x-test-identity", b"owner")]))[0], 200)
        client = app.state.kiro_agentcore.sessions["session-a"].client
        result = await invoke_native(client._native_hook_bridge, {
            "hook_event_name": "PreToolUse", "session_id": client.session_id,
            "tool_name": "write", "tool_input": {}})
        self.assertEqual(result, {"block": True, "reason": "policy"})
        self.assertEqual(seen[0]["message"], "PRIVATE_WARNING")
        self.assertNotIn("PRIVATE_WARNING", self.log.read_text())

    async def test_compact_timeout_discards_uncertain_client(self):
        async def authorizer(req, session_id):
            return True
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "slow", cli_path=str(FAKE), api_key="synthetic",
                                 startup_timeout=2, turn_timeout=10)
        app = create_agentcore_app(config, session_authorizer=authorizer,
            options_factory=lambda workspace, resume: KiroAgentOptions(
                tools=[], env={"FAKE_COMPACT_HANG": "1", "FAKE_LOG_REQUESTS": str(self.log)}))
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "hello"}))[0], 200)
        status, body, _ = await self.invoke(app, {"action": "compact"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        session = app.state.kiro_agentcore.sessions["session-a"]
        self.assertFalse(session.broken)
        self.assertIsNone(session.client)

    async def test_mapped_agent_profile_is_verified_and_recreated_in_agentcore(self):
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        def factory(workspace, resume):
            definition = AgentDefinition(
                "Review", "Inspect input.", tools=["Read"], tools_mode="selectors",
                tool_selector_map={"Read": ["read"]}, disallowed_tools=["Read(secret)"],
                disallowed_tools_mode="v3-permissions",
                disallowed_tool_permission_map={"Read(secret)": [
                    {"capability": "fs_read", "effect": "deny", "match": [str(workspace / "secret")]}]})
            return KiroAgentOptions(agent="worker", agents={"worker": definition})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        status, body, _ = await self.invoke(app, {"prompt": "hello"}, headers=owner)
        self.assertEqual((status, json.loads(body)["response"]), (200, "hello"))
        session = app.state.kiro_agentcore.sessions["session-a"]
        profile = session.directory / "home" / ".kiro" / "agents" / "worker.json"
        config = json.loads(profile.read_text())
        self.assertEqual(config["tools"], ["read"])
        self.assertEqual(config["permissions"]["rules"][0]["effect"], "deny")
        await app.state.kiro_agentcore.close()
        restarted = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(restarted)
        status, body, _ = await self.invoke(restarted, {"prompt": "resumed"}, headers=owner)
        self.assertEqual((status, json.loads(body)["response"]), (200, "resumed"))

    async def test_hook_listing_is_available_to_authorized_agentcore_application(self):
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], env={"FAKE_HOOK_EVENTS": "1"})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        self.assertEqual((await self.invoke(app, {"prompt": "hello"}, headers=owner))[0], 200)
        client = app.state.kiro_agentcore.sessions["session-a"].client
        listing = await client.list_kiro_hooks(trigger="PreToolUse")
        self.assertEqual([hook["id"] for hook in listing["hooks"]], ["hook-1"])
        action = {"action": "list_kiro_hooks"}
        self.assertEqual((await self.invoke(app, action,
                                            headers=[(b"x-test-identity", b"other")]))[0], 403)
        self.assertEqual((await self.invoke(app, action, headers=owner))[0], 403)

    async def test_available_commands_in_authorized_agentcore_session(self):
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        commands = [{"name": "review", "description": "Review", "input": {"hint": "[path]"}}]
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], env={"FAKE_STARTUP_COMMANDS": json.dumps(commands)})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        self.assertEqual((await self.invoke(app, {"prompt": "hello"}, headers=owner))[0], 200)
        client = app.state.kiro_agentcore.sessions["session-a"].client
        self.assertEqual(await client.get_kiro_available_commands(), commands)
        self.assertEqual((await client.get_server_info())["commands"], commands)
        action = {"action": "available_commands"}
        self.assertEqual((await self.invoke(app, action, headers=[(b"x-test-identity", b"other")]))[0], 403)
        self.assertEqual((await self.invoke(app, action, headers=owner))[0], 403)

    async def test_reported_subagent_permission_context_in_authorized_agentcore_session(self):
        seen = []
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        async def pretool(event, tool_id, context):
            seen.append(("hook", event.get("agent_id"), event.get("agent_type"),
                         event.get("kiro_observed_agent_type")))
            return {}
        async def allow(name, data, context):
            seen.append(("callback", context.agent_id, context.kiro_agent_type))
            return PermissionResultAllow()
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], can_use_tool=allow,
                hooks={"preToolUse": [HookMatcher(hooks=[pretool])]},
                env={"FAKE_PERMISSION_TOOL_META": json.dumps({"kiro": {"agentSubtaskId": "child-1"}}),
                     "FAKE_PERMISSION_AGENT_TYPE": "reviewer"})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "permission"},
                                            headers=[(b"x-test-identity", b"other")]))[0], 403)
        status, body, _ = await self.invoke(app, {"prompt": "permission"},
                                            headers=[(b"x-test-identity", b"owner")])
        self.assertEqual((status, json.loads(body)["response"]), (200, "yes"))
        self.assertEqual(seen, [("hook", "child-1", "reviewer", True),
                                ("callback", "child-1", "reviewer")])

    async def test_sdk_mcp_output_transform_in_authorized_agentcore_session(self):
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        @tool("probe", "Test output", {})
        async def probe(args):
            return {"content": [{"type": "text", "text": "ORIGINAL"}]}
        async def transform(server, name, arguments, result):
            return {"content": [{"type": "text", "text": "REPLACED"}]}
        def factory(workspace, resume):
            return KiroAgentOptions(mcp_servers={"probe": create_mcp_server("probe", tools=[probe])},
                                    sdk_mcp_output_transforms={"probe": transform})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "hello"},
                                            headers=[(b"x-test-identity", b"other")]))[0], 403)
        status, body, _ = await self.invoke(app, {"prompt": "hello"},
                                            headers=[(b"x-test-identity", b"owner")])
        self.assertEqual((status, json.loads(body)["response"]), (200, "hello"))
        bridge = app.state.kiro_agentcore.sessions["session-a"].client._named_mcp_bridges["probe"][0]
        response = await bridge._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "probe", "arguments": {}}})
        self.assertEqual(response["result"]["content"][0]["text"], "REPLACED")

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP dependency")
    async def test_official_mcp_output_transform_in_authorized_agentcore_session(self):
        import mcp.types as types
        from mcp.server import Server
        info = types.Tool(name="probe", description="Probe", inputSchema={"type": "object"})
        if hasattr(types, "jsonrpc_message_adapter"):
            async def list_tools(ctx, params):
                return types.ListToolsResult(tools=[info])
            async def call_tool(ctx, params):
                return types.CallToolResult(content=[types.TextContent(type="text", text="ORIGINAL")])
            server = Server("probe", on_list_tools=list_tools, on_call_tool=call_tool)
        else:
            server = Server("probe")
            @server.list_tools()
            async def list_tools():
                return [info]
            @server.call_tool()
            async def call_tool(name, arguments):
                return [types.TextContent(type="text", text="ORIGINAL")]
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        async def transform(server_name, name, arguments, result):
            return {"content": [{"type": "text", "text": "REPLACED"}]}
        def factory(workspace, resume):
            return KiroAgentOptions(mcp_servers={"probe": {"type": "sdk", "name": "probe", "instance": server}},
                                    sdk_mcp_output_transforms={"probe": transform})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "hello"},
                                            headers=[(b"x-test-identity", b"other")]))[0], 403)
        status, body, _ = await self.invoke(app, {"prompt": "hello"},
                                            headers=[(b"x-test-identity", b"owner")])
        self.assertEqual((status, json.loads(body)["response"]), (200, "hello"))
        bridge = app.state.kiro_agentcore.sessions["session-a"].client._named_mcp_bridges["probe"][0]
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.server.sockets[0].getsockname()[1])
        async def exchange(value):
            writer.write((json.dumps(value) + "\n").encode())
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 5))
        try:
            writer.write((bridge.token + "\n").encode())
            initialized = await exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1"}}})
            self.assertIn("result", initialized)
            writer.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await writer.drain()
            response = await exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "probe", "arguments": {}}})
            self.assertEqual(response["result"]["content"][0]["text"], "REPLACED")
        finally:
            writer.close()
            await writer.wait_closed()

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP dependency")
    async def test_remote_mcp_output_transform_in_authorized_agentcore_session(self):
        from test_remote_mcp_toggle import TestMcpHttpServer
        service = TestMcpHttpServer("http")
        url = await service.start()
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        async def transform(server, name, arguments, result):
            return {"content": [{"type": "text", "text": "REPLACED"}]}
        def factory(workspace, resume):
            return KiroAgentOptions(
                mcp_servers={"probe": {"type": "http", "url": url,
                    "headers": {"Authorization": "Bearer fixture-secret"}}},
                sdk_mcp_toggle_mode="sdk-all", sdk_mcp_output_transforms={"probe": transform})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        try:
            self.assertEqual((await self.invoke(app, {"prompt": "hello"},
                                                headers=[(b"x-test-identity", b"other")]))[0], 403)
            status, body, _ = await self.invoke(app, {"prompt": "hello"},
                                                headers=[(b"x-test-identity", b"owner")])
            self.assertEqual((status, json.loads(body)["response"]), (200, "hello"))
            bridge = app.state.kiro_agentcore.sessions["session-a"].client._named_external_mcp_bridges["probe"][0]
            reader, writer = await asyncio.open_connection("127.0.0.1", bridge.server.sockets[0].getsockname()[1])
            async def exchange(value):
                writer.write((json.dumps(value) + "\n").encode())
                await writer.drain()
                return json.loads(await asyncio.wait_for(reader.readline(), 8))
            try:
                writer.write((bridge.token + "\n").encode())
                initialized = await exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "1"}}})
                self.assertIn("result", initialized)
                writer.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                await writer.drain()
                response = await exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                            "params": {"name": "probe", "arguments": {}}})
                self.assertEqual(response["result"]["content"][0]["text"], "REPLACED")
                self.assertFalse(service.bad_auth)
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await app.state.kiro_agentcore.close()
            await service.close()

    async def test_native_sdk_stop_hook_in_authorized_agentcore_session(self):
        from test_sdk_native_pretool import invoke_native
        seen = []
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        async def stop(event, tool_id, context):
            seen.append(event["continued"])
            return {'continue': "CONTINUE"}
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], hooks={"agentStop": [HookMatcher(hooks=[stop])]},
                                    native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native")
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "cancel"},
                                            headers=[(b"x-test-identity", b"other")]))[0], 403)
        owner = [(b"x-test-identity", b"owner")]
        turn = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}, headers=owner))
        try:
            session = await self.wait_for_active_turn(app)
            response = await invoke_native(session.client._native_hook_bridge, {
                "hook_event_name": "Stop", "session_id": session.client.session_id})
            self.assertEqual(response, {"block": True, "reason": "CONTINUE"})
            self.assertEqual((await self.invoke(app, {"action": "interrupt"}, headers=owner))[0], 200)
            status, body, _ = await turn
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["is_error"])
            self.assertEqual(json.loads(body)["stop_reason"], "cancelled")
            self.assertEqual(seen, [False])
        finally:
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)

    async def test_native_sdk_stop_hook_failure_is_sanitized_in_agentcore(self):
        from test_sdk_native_pretool import invoke_native
        async def authorizer(req, session_id):
            return True
        async def stop(event, tool_id, context):
            raise RuntimeError("PRIVATE_STOP_DETAIL")
        app = create_agentcore_app(self.config, session_authorizer=authorizer,
            options_factory=lambda workspace, resume: KiroAgentOptions(
                tools=[], hooks={"agentStop": [HookMatcher(hooks=[stop])]}, native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native"))
        self.apps.append(app)
        turn = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}))
        try:
            session = await self.wait_for_active_turn(app)
            response = await invoke_native(session.client._native_hook_bridge, {
                "hook_event_name": "Stop", "session_id": session.client.session_id})
            self.assertFalse(response["block"])
            self.assertNotIn("PRIVATE_STOP_DETAIL", str(response))
            status, body, _ = await turn
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["is_error"])
            self.assertNotIn("PRIVATE_STOP_DETAIL", body.decode())
        finally:
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)

    async def test_agent_initial_prompt_finishes_before_user_request(self):
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        def factory(workspace, resume):
            return KiroAgentOptions(agent="selected", env={"FAKE_LOG_REQUESTS": str(self.log)},
                agents={"selected": AgentDefinition("Selected", "Follow the task", initial_prompt="initial")})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        status, body, _ = await self.invoke(app, {"prompt": "hello"}, headers=owner)
        self.assertEqual((status, json.loads(body)["response"]), (200, "hello"))
        status, body, _ = await self.invoke(app, {"prompt": "later"}, headers=owner)
        self.assertEqual((status, json.loads(body)["response"]), (200, "later"))
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        prompts = [item["params"]["prompt"][0]["text"] for item in requests if item.get("method") == "session/prompt"]
        self.assertEqual(prompts, ["initial", "hello", "later"])

    async def test_agent_initial_prompt_is_not_mixed_into_user_stream(self):
        async def authorizer(req, session_id):
            return True
        def factory(workspace, resume):
            return KiroAgentOptions(agent="selected",
                agents={"selected": AgentDefinition("Selected", "Follow the task", initial_prompt="initial")})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        status, body, _ = await self.invoke(app, {"prompt": "hello", "stream": True})
        self.assertEqual(status, 200)
        events = [json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual("".join(item["text"] for item in events if item.get("type") == "text_delta"), "hello")
        self.assertEqual(events[-1]["response"], "hello")
        self.assertNotIn("initial", body.decode())

    async def test_failed_initial_prompt_never_sends_user_prompt(self):
        async def authorizer(req, session_id):
            return True
        def factory(workspace, resume):
            return KiroAgentOptions(agent="selected", env={"FAKE_LOG_REQUESTS": str(self.log),
                "FAKE_STOP_REASON": "cancelled"},
                agents={"selected": AgentDefinition("Selected", "Follow the task", initial_prompt="initial")})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        status, body, _ = await self.invoke(app, {"prompt": "must-not-run"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        prompts = [item["params"]["prompt"][0]["text"] for item in requests if item.get("method") == "session/prompt"]
        self.assertEqual(prompts, ["initial"])

    async def test_code_intelligence_controls_require_owner_and_idle_enabled_session(self):
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner" and session_id == "session-a"

        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], enable_kiro_code_intelligence=True,
                                    env={"FAKE_LOG_REQUESTS": str(self.log)})

        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        self.assertEqual((await self.invoke(app, {"action": "code_status"}, headers=owner))[0], 409)
        self.assertEqual((await self.invoke(app, {"prompt": "first"}, headers=owner))[0], 200)
        session = app.state.kiro_agentcore.sessions["session-a"]
        for action, subcommand in (("code_status", "status"), ("code_init", "init"),
                                   ("code_overview", "overview")):
            with self.subTest(action=action):
                self.assertEqual((await self.invoke(app, {"action": action}, headers=[
                    (b"x-test-identity", b"other")]))[0], 403)
                status, body, headers = await self.invoke(app, {"action": action}, headers=owner)
                self.assertEqual((status, json.loads(body)), (200, {
                    "action": action, "result": {"success": True, "subcommand": subcommand}, "is_error": False}))
                self.assertEqual(headers.get(b"cache-control"), b"no-store")
        await session.lock.acquire()
        try:
            self.assertEqual((await self.invoke(app, {"action": "code_init"}, headers=owner))[0], 409)
        finally:
            session.lock.release()
        self.assertEqual((await self.invoke(app, {"action": "code_init", "extra": True}, headers=owner))[0], 400)
        self.assertFalse(session.lock.locked())
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_status():
            started.set()
            await release.wait()
            return {"success": True}

        with patch.object(session.client, "get_kiro_code_intelligence_status", slow_status):
            pending = asyncio.create_task(self.invoke(app, {"action": "code_status"}, headers=owner))
            try:
                await asyncio.wait_for(started.wait(), 5)
                self.assertEqual((await self.invoke(app, {"action": "code_init"}, headers=owner))[0], 409)
                self.assertEqual((await self.invoke(app, {"prompt": "parallel"}, headers=owner))[0], 409)
            finally:
                release.set()
            self.assertEqual((await pending)[0], 200)

        async def rejected():
            return {"success": False, "message": "Initialization failed"}

        with patch.object(session.client, "initialize_kiro_code_intelligence", rejected):
            status, body, _ = await self.invoke(app, {"action": "code_init"}, headers=owner)
        self.assertEqual((status, json.loads(body)["is_error"]), (200, True))

        no_authorizer = self.app(factory=factory)
        self.assertEqual((await self.invoke(no_authorizer, {"action": "code_status"}))[0], 403)
        disabled = create_agentcore_app(self.config, session_authorizer=authorizer,
            options_factory=lambda workspace, resume: KiroAgentOptions(tools=[]))
        self.apps.append(disabled)
        self.assertEqual((await self.invoke(disabled, {"prompt": "first"}, headers=owner))[0], 200)
        self.assertEqual((await self.invoke(disabled, {"action": "code_status"}, headers=owner))[0], 400)

    async def test_code_intelligence_control_failure_and_timeout_discard_client(self):
        from dataclasses import replace

        async def authorizer(req, session_id):
            return True

        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], enable_kiro_code_intelligence=True)

        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        for mode in ("error", "timeout"):
            with self.subTest(mode=mode):
                self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
                service = app.state.kiro_agentcore
                session = service.sessions["session-a"]

                async def failing():
                    if mode == "timeout":
                        await asyncio.Event().wait()
                    raise RuntimeError("private-code-detail")

                if mode == "timeout":
                    service.config = replace(self.config, startup_timeout=0.2)
                with patch.object(session.client, "initialize_kiro_code_intelligence", failing):
                    result = await self.invoke(app, {"action": "code_init"})
                self.assert_agent_failure(result)
                self.assertNotIn(b"private-code-detail", result[1])
                self.assertIsNone(session.client)
                self.assertFalse(session.lock.locked())
                service.config = self.config

    async def test_session_authorizer_fails_closed_on_errors_timeout_and_non_boolean(self):
        from dataclasses import replace

        for outcome in ("error", "timeout", "non_boolean", "cancelled"):
            async def authorizer(req, session_id):
                if outcome == "error":
                    raise RuntimeError("secret must not be returned")
                if outcome == "timeout":
                    await asyncio.sleep(1)
                if outcome == "cancelled":
                    raise asyncio.CancelledError()
                return "yes"

            app = create_agentcore_app(replace(self.config, session_authorization_timeout=0.01),
                                       session_authorizer=authorizer)
            self.apps.append(app)
            with self.subTest(outcome=outcome):
                status, body, _ = await self.invoke(app, {"prompt": "x"})
                self.assertEqual((status, json.loads(body)), (403, {"error": "Session access denied"}))
                self.assertFalse(app.state.kiro_agentcore.sessions)
        with self.assertRaisesRegex(ValueError, "callable"):
            create_agentcore_app(self.config, session_authorizer=True)

    async def test_authorizer_timeout_does_not_wait_for_cancellation(self):
        from dataclasses import replace
        release = asyncio.Event()
        finished = asyncio.Event()

        async def authorizer(req, session_id):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished.set()
            return True

        app = create_agentcore_app(replace(self.config, session_authorization_timeout=0.01),
                                   session_authorizer=authorizer)
        self.apps.append(app)
        try:
            status, _, _ = await self.invoke(app, {"prompt": "x"})
            self.assertEqual(status, 403)
            self.assertFalse(app.state.kiro_agentcore.sessions)
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), 1)

    async def test_registered_owner_is_required_for_session_access(self):
        from kiro_agent_sdk import SQLiteSessionOwnership

        ownership = SQLiteSessionOwnership(self.root / "owners.sqlite")
        await ownership.register("session-a", "alice")

        async def authorizer(req, session_id):
            # In a real deployment this must be a cryptographically verified identity.
            identity = req.headers.get("x-test-identity", "")
            return await ownership.is_owner(session_id, identity) if identity else False

        app = create_agentcore_app(self.config, session_authorizer=authorizer,
            options_factory=lambda workspace, resume: KiroAgentOptions(tools=[]))
        self.apps.append(app)
        for identity, session, expected in (("alice", b"session-a", 200),
                                             ("bob", b"session-a", 403),
                                             ("alice", b"session-b", 403)):
            status, _, _ = await self.invoke(app, {"prompt": "hello"}, session,
                                              [(b"x-test-identity", identity.encode())])
            self.assertEqual(status, expected)

    async def invoke(self, app, payload, runtime_id=b"session-a", headers=()):
        return await request(app, "POST", "/invocations", json.dumps(payload).encode(),
                             [(HEADER, runtime_id), *headers])

    def assert_agent_failure(self, result):
        status, body, _ = result
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertIn("error", json.loads(body))

    async def wait_for_active_turn(self, app, runtime_id="session-a"):
        async def wait():
            while True:
                session = app.state.kiro_agentcore.sessions.get(runtime_id)
                if session and session.turn_ready and session.client and session.client._turn and not session.client._turn.done():
                    return session
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(wait(), 10)

    async def wait_for_interaction(self, app, runtime_id=b"session-a"):
        async def wait():
            while True:
                status, body, _ = await self.invoke(app, {"action": "interactions"}, runtime_id)
                self.assertEqual(status, 200)
                pending = json.loads(body)["pending"]
                if pending:
                    return pending[0]
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(wait(), 10)

    async def test_json_turns_resume_and_session_isolation(self):
        app = self.app()
        status, body, _ = await request(app, "GET", "/ping")
        self.assertEqual((status, json.loads(body)), (200, {"status": "Healthy"}))
        for runtime_id, prompt in ((b"session-a", "first"), (b"session-a", "second"),
                                   (b"session-b", "other")):
            status, body, _ = await self.invoke(app, {"prompt": prompt}, runtime_id)
            data = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual((data["response"], data["session_id"]), (prompt, runtime_id.decode()))
            self.assertFalse(data["is_error"])
        entries = app.state.kiro_agentcore.sessions
        self.assertNotEqual(entries["session-a"].directory, entries["session-b"].directory)
        for session in entries.values():
            self.assertEqual(session.client.options.env["KIRO_API_KEY"], "synthetic-test-key")
            self.assertEqual(session.client.options.env["HOME"], str(session.directory / "home"))
            self.assertNotIn("synthetic-test-key", (session.directory / "session.json").read_text())
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(sum(item["method"] == "session/new" for item in requests), 2)
        await app.state.kiro_agentcore.close()
        resumed = self.app()
        status, body, _ = await self.invoke(resumed, {"prompt": "after restart"})
        self.assertEqual((status, json.loads(body)["response"]), (200, "after restart"))
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(any(item["method"] == "session/load" for item in requests))

    async def test_streaming_contains_text_then_terminal_result(self):
        app = self.app()
        status, body, headers = await self.invoke(app, {"prompt": "streaming", "stream": True})
        self.assertEqual(status, 200)
        self.assertTrue(headers[b"content-type"].startswith(b"text/event-stream"))
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b"data: ")]
        self.assertEqual("".join(event["text"] for event in events if event["type"] == "text_delta"),
                         "streaming")
        self.assertEqual((events[-1]["type"], events[-1]["response"]), ("result", "streaming"))
        status, body, _ = await self.invoke(app, {"prompt": "next"})
        self.assertEqual((status, json.loads(body)["response"]), (200, "next"))

    async def test_stream_does_not_mix_forwarded_subagent_text_into_root(self):
        app = self.app(factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], forward_subagent_text=True))
        status, body, _ = await self.invoke(app, {"prompt": "nested", "stream": True})
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b"data: ")]
        text = "".join(event["text"] for event in events if event["type"] == "text_delta")
        self.assertEqual(status, 200)
        self.assertEqual((text, events[-1]["response"]), ("ROOT done", "ROOT done"))
        self.assertNotIn("CHILD_TEXT", body.decode())

    async def test_sdk_events_require_explicit_server_and_request_opt_in(self):
        default = self.app()
        self.assertEqual((await self.invoke(default, {
            "prompt": "tool", "stream": True, "include_events": True}))[0], 403)
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "events", cli_path=str(FAKE),
                                 api_key="synthetic", allow_sdk_events=True)
        app = self.app(config)
        self.assertEqual((await self.invoke(app, {
            "prompt": "tool", "include_events": True}))[0], 400)
        status, body, _ = await self.invoke(app, {
            "prompt": "tool", "stream": True, "include_events": True})
        self.assertEqual(status, 200)
        events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b"data: ")]
        names = {event["event_type"] for event in events if event["type"] == "sdk_event"}
        self.assertTrue({"StreamEvent", "AgentMessage", "ToolCall", "ResultMessage"} <= names)
        self.assertEqual(events[-1]["type"], "result")

    @unittest.skipUnless(os.name == "posix", "POSIX process users are required")
    async def test_selected_process_user_in_authorized_agentcore_session(self):
        import pwd
        account = pwd.getpwuid(os.geteuid()).pw_name

        async def authorizer(request, session_id):
            return request.headers.get("x-test-identity") == "owner"

        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], user=account)

        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        payload = {"prompt": "process_uid_probe"}
        self.assertEqual((await self.invoke(app, payload,
                                           headers=[(b"x-test-identity", b"other")]))[0], 403)
        self.assertEqual((await self.invoke(app, {**payload, "user": "other"},
                                           headers=[(b"x-test-identity", b"owner")]))[0], 400)
        status, body, _ = await self.invoke(app, payload, headers=[(b"x-test-identity", b"owner")])
        self.assertEqual((status, json.loads(body)["response"]), (200, str(os.geteuid())))

    async def test_hook_on_failure_blocks_only_authorized_session_request(self):
        calls = []
        async def fail(event, tool_id, context):
            calls.append(event["tool_name"])
            raise RuntimeError("private hook failure")
        async def allow(name, data, context):
            return PermissionResultAllow()
        def factory(workspace, resume):
            return KiroAgentOptions(
                tools=[], can_use_tool=allow,
                hooks={"preToolUse": [HookMatcher(hooks=[fail], on_failure="block")]})
        async def authorizer(request, session_id):
            return request.headers.get("x-test-identity") == "owner"
        no_authorizer = create_agentcore_app(self.config, options_factory=factory)
        self.apps.append(no_authorizer)
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assert_agent_failure(await self.invoke(no_authorizer, {"prompt": "permission"}))
        self.assertEqual(calls, [])
        self.assertEqual((await self.invoke(app, {"prompt": "permission"},
                                           headers=[(b"x-test-identity", b"other")]))[0], 403)
        self.assertEqual(calls, [])
        status, body, _ = await self.invoke(app, {"prompt": "permission"},
                                             headers=[(b"x-test-identity", b"owner")])
        self.assertEqual((status, json.loads(body)["response"]), (200, "no"))
        self.assertEqual(calls, ["write"])
        self.assertNotIn(b"private hook failure", body)

    async def test_hook_on_failure_continue_rechecks_owner_before_allow(self):
        started, release = asyncio.Event(), asyncio.Event()
        permitted = True
        async def fail(event, tool_id, context):
            started.set()
            await release.wait()
            raise RuntimeError("private hook failure")
        async def allow(name, data, context):
            return PermissionResultAllow()
        def factory(workspace, resume):
            return KiroAgentOptions(
                tools=[], can_use_tool=allow, env={"FAKE_LOG_REQUESTS": str(self.log)},
                hooks={"preToolUse": [HookMatcher(hooks=[fail], on_failure="continue")]})
        async def authorizer(request, session_id):
            return permitted and request.headers.get("x-test-identity") == "owner"
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        pending = asyncio.create_task(self.invoke(app, {"prompt": "permission"},
                                                   headers=[(b"x-test-identity", b"owner")]))
        try:
            await asyncio.wait_for(started.wait(), 5)
            permitted = False
            release.set()
            status, body, _ = await asyncio.wait_for(pending, 10)
            self.assertIn(status, (200, 403))
            self.assertNotIn(b'"response":"yes"', body)
            messages = [json.loads(line) for line in self.log.read_text().splitlines()]
            self.assertFalse(any(message.get("id") == 99 and message.get("result", {}).get(
                "outcome", {}).get("optionId") == "yes" for message in messages))
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    @unittest.skipUnless(os.name == "posix", "native Kiro hooks require POSIX")
    async def test_native_hook_failure_policy_rechecks_session_owner(self):
        permitted = True
        permission_started, release_permission = asyncio.Event(), asyncio.Event()
        hook_started, release_hook = asyncio.Event(), asyncio.Event()
        async def native(*args):
            hook_started.set()
            await release_hook.wait()
            return {}
        async def allow(name, data, context):
            permission_started.set()
            await release_permission.wait()
            return PermissionResultAllow()
        def factory(workspace, resume):
            return KiroAgentOptions(
                tools=[], can_use_tool=allow,
                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[native], on_failure="block")]})
        async def authorizer(request, session_id):
            return permitted and request.headers.get("x-test-identity") == "owner"
        no_authorizer = create_agentcore_app(self.config, options_factory=factory)
        self.apps.append(no_authorizer)
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        invocation = writer = None
        try:
            self.assert_agent_failure(await self.invoke(no_authorizer, {"prompt": "permission"}))
            invocation = asyncio.create_task(self.invoke(
                app, {"prompt": "permission"}, headers=[(b"x-test-identity", b"owner")]))
            await asyncio.wait_for(permission_started.wait(), 5)
            client = app.state.kiro_agentcore.sessions["session-a"].client
            bridge = client._native_hook_bridge
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", bridge.server.sockets[0].getsockname()[1])
            writer.write((bridge.token + "\n" + json.dumps({
                "hook_event_name": "PreToolUse", "tool_name": "write",
                "session_id": client.session_id}) + "\n").encode())
            await writer.drain()
            await asyncio.wait_for(hook_started.wait(), 5)
            permitted = False
            release_hook.set()
            self.assertTrue(json.loads(await asyncio.wait_for(reader.readline(), 5))["block"])
            release_permission.set()
            _, body, _ = await asyncio.wait_for(invocation, 10)
            self.assertNotIn(b'"response":"yes"', body)
        finally:
            release_hook.set()
            release_permission.set()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if invocation is not None and not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)

    @unittest.skipUnless(os.name == "posix", "native Kiro hooks require POSIX")
    async def test_native_continue_policy_cannot_bypass_revoked_owner(self):
        permitted = True
        permission_started, release_permission = asyncio.Event(), asyncio.Event()
        hook_started, release_hook = asyncio.Event(), asyncio.Event()
        async def native(*args):
            hook_started.set()
            await release_hook.wait()
            raise RuntimeError("private failure")
        async def allow(name, data, context):
            permission_started.set()
            await release_permission.wait()
            return PermissionResultAllow()
        def factory(workspace, resume):
            return KiroAgentOptions(
                tools=[], can_use_tool=allow,
                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[native], timeout=2, on_failure="continue")]})
        async def authorizer(request, session_id):
            return permitted and request.headers.get("x-test-identity") == "owner"
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        invocation = writer = None
        try:
            invocation = asyncio.create_task(self.invoke(
                app, {"prompt": "permission"}, headers=[(b"x-test-identity", b"owner")]))
            await asyncio.wait_for(permission_started.wait(), 5)
            client = app.state.kiro_agentcore.sessions["session-a"].client
            bridge = client._native_hook_bridge
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", bridge.server.sockets[0].getsockname()[1])
            writer.write((bridge.token + "\n" + json.dumps({
                "hook_event_name": "PreToolUse", "tool_name": "write",
                "session_id": client.session_id}) + "\n").encode())
            await writer.drain()
            await asyncio.wait_for(hook_started.wait(), 5)
            permitted = False
            release_hook.set()
            self.assertTrue(json.loads(await asyncio.wait_for(reader.readline(), 5))["block"])
            release_permission.set()
            _, body, _ = await asyncio.wait_for(invocation, 10)
            self.assertNotIn(b'"response":"yes"', body)
        finally:
            release_hook.set()
            release_permission.set()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if invocation is not None and not invocation.done():
                invocation.cancel()
                await asyncio.gather(invocation, return_exceptions=True)

    async def test_observed_posttool_failure_block_requires_owner_and_hides_failure(self):
        calls = []
        async def fail(event, tool_id, context):
            calls.append(event["tool_name"])
            raise RuntimeError("private failure")
        def factory(workspace, resume):
            return KiroAgentOptions(
                tools=[], hooks={"postToolUse": [HookMatcher(hooks=[fail], on_failure="block")]})
        async def authorizer(request, session_id):
            return request.headers.get("x-test-identity") == "owner"
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "tool"},
                                           headers=[(b"x-test-identity", b"other")]))[0], 403)
        self.assertEqual(calls, [])
        status, body, _ = await self.invoke(app, {"prompt": "tool"},
                                             headers=[(b"x-test-identity", b"owner")])
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(b"private failure", body)
        self.assertEqual(calls, ["read"])

    @unittest.skipUnless(os.name == "posix", "native Kiro hooks require POSIX")
    async def test_native_stop_failure_policy_cannot_bypass_revoked_owner(self):
        started, release = asyncio.Event(), asyncio.Event()
        permission_started, permission_release = asyncio.Event(), asyncio.Event()
        permitted = True
        async def stop(*args):
            started.set()
            await release.wait()
            raise RuntimeError("private failure")
        async def allow(name, data, context):
            permission_started.set()
            await permission_release.wait()
            return PermissionResultAllow()
        def factory(workspace, resume):
            return KiroAgentOptions(
                tools=[], can_use_tool=allow, native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                hooks={"agentStop": [HookMatcher(hooks=[stop], timeout=2, on_failure="block")]})
        async def authorizer(request, session_id):
            return permitted and request.headers.get("x-test-identity") == "owner"
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        pending = writer = None
        try:
            pending = asyncio.create_task(self.invoke(
                app, {"prompt": "permission"}, headers=[(b"x-test-identity", b"owner")]))
            await asyncio.wait_for(permission_started.wait(), 5)
            client = app.state.kiro_agentcore.sessions["session-a"].client
            bridge = client._native_hook_bridge
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", bridge.server.sockets[0].getsockname()[1])
            writer.write((bridge.token + "\n" + json.dumps({
                "hook_event_name": "Stop", "session_id": client.session_id}) + "\n").encode())
            await writer.drain()
            await asyncio.wait_for(started.wait(), 5)
            permitted = False
            release.set()
            self.assertFalse(json.loads(await asyncio.wait_for(reader.readline(), 5))["block"])
            permission_release.set()
            _, body, _ = await asyncio.wait_for(pending, 10)
            self.assertNotIn(b'"response":"yes"', body)
            self.assertNotIn(b"private failure", body)
        finally:
            release.set()
            permission_release.set()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    @unittest.skipUnless(os.name == "posix", "native Kiro hooks require POSIX")
    async def test_multiple_native_stop_hooks_recheck_revoked_owner(self):
        started, release = asyncio.Event(), asyncio.Event()
        permission_started, permission_release = asyncio.Event(), asyncio.Event()
        permitted = True
        calls, warnings = [], []

        async def first(*args):
            calls.append("first")
            started.set()
            await release.wait()
            return {'warning': "private warning"}

        async def second(*args):
            calls.append("second")
            return {}

        async def notice(event):
            warnings.append(event)

        async def allow(name, data, context):
            permission_started.set()
            await permission_release.wait()
            return PermissionResultAllow()

        def factory(workspace, resume):
            return KiroAgentOptions(
                tools=[], can_use_tool=allow, native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                on_native_hook_system_message=notice,
                hooks={"agentStop": [HookMatcher(hooks=[first, second], timeout=2, on_failure="continue")]})

        async def authorizer(request, session_id):
            return permitted and request.headers.get("x-test-identity") == "owner"

        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        pending = writer = None
        try:
            pending = asyncio.create_task(self.invoke(
                app, {"prompt": "permission"}, headers=[(b"x-test-identity", b"owner")]))
            await asyncio.wait_for(permission_started.wait(), 5)
            client = app.state.kiro_agentcore.sessions["session-a"].client
            bridge = client._native_hook_bridge
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", bridge.server.sockets[0].getsockname()[1])
            writer.write((bridge.token + "\n" + json.dumps({
                "hook_event_name": "Stop", "session_id": client.session_id}) + "\n").encode())
            await writer.drain()
            await asyncio.wait_for(started.wait(), 5)
            permitted = False
            release.set()
            self.assertFalse(json.loads(await asyncio.wait_for(reader.readline(), 5))["block"])
            self.assertEqual(calls, ["first"])
            self.assertEqual(warnings, [])
            permission_release.set()
            _, body, _ = await asyncio.wait_for(pending, 10)
            self.assertNotIn(b"private warning", body)
        finally:
            release.set()
            permission_release.set()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_input_validation_limits_and_no_request_credentials(self):
        app = self.app()
        status, _, _ = await request(app, "POST", "/invocations", b'{"prompt":"x"}')
        self.assertEqual(status, 400)
        for body in (b'{"prompt":"x","api_key":"secret"}', b'{"prompt":"x","stream":1}',
                     b'{"prompt":"x","prompt":"y"}', b'{"prompt":"\\ud800"}',
                     b'{"prompt":' + b"9" * 10000 + b"}",
                     b'{"prompt":' + b"[" * 1100 + b'"x"' + b"]" * 1100 + b"}"):
            status, response, _ = await request(app, "POST", "/invocations", body, [(HEADER, b"session-a")])
            self.assertEqual(status, 400)
            self.assertNotIn(b"secret", response)
        limited = self.app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "small", cli_path=str(FAKE),
                                           api_key="synthetic", max_body_bytes=12))
        status, _, _ = await self.invoke(limited, {"prompt": "too long"})
        self.assertEqual(status, 413)
        status, _, _ = await self.invoke(app, {"prompt": "x"}, b"../escape")
        self.assertEqual(status, 400)

    async def test_image_and_document_blocks_are_forwarded_as_acp(self):
        app = self.app(factory=lambda workspace, resume: KiroAgentOptions(tools=[], env={
            "FAKE_LOG_REQUESTS": str(self.log), "FAKE_PROMPT_CAPABILITIES": "1"}))
        blocks = [
            {"type": "text", "text": "read these"},
            {"type": "image", "mimeType": "image/png", "data": "AA=="},
            {"type": "resource", "resource": {"uri": "memory://file.pdf", "mimeType": "application/pdf",
                                              "blob": "AA=="}},
            {"type": "resource", "resource": {"uri": "memory://notes.txt", "mimeType": "text/plain",
                                              "text": "hello"}},
        ]
        for stream in (False, True):
            status, body, _ = await self.invoke(app, {"prompt": blocks, "stream": stream})
            self.assertEqual(status, 200, body)
            self.assertIn(b"read these", body)
        entries = [json.loads(line) for line in self.log.read_text().splitlines()]
        prompts = [entry["params"]["prompt"] for entry in entries if entry["method"] == "session/prompt"]
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:
            self.assertEqual(prompt[1], {"type": "image", "mimeType": "image/png", "data": "AA=="})
            self.assertEqual(prompt[2]["resource"]["mimeType"], "application/pdf")
            self.assertEqual(prompt[2]["resource"]["blob"], "AA==")
            self.assertEqual(prompt[3]["resource"]["text"], "hello")

    async def test_unsafe_or_oversized_blocks_are_rejected_before_startup(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "blocks", cli_path=str(FAKE),
                                 api_key="synthetic", max_prompt_bytes=160)
        app = self.app(config)
        bad = [
            [],
            [{"type": "text", "text": ""}],
            [{"type": "text", "text": "hello", "extra": "ignored"}],
            [{"type": "resource_link", "uri": "file:///etc/passwd", "name": "passwd"}],
            [{"type": "resource", "resource": {"uri": "file:///etc/passwd", "text": "x"}}],
            [{"type": "image", "mimeType": "image/png", "data": "AA==", "uri": "file:///etc/passwd"}],
            [{"type": "image", "mimeType": "image/png", "data": "%%"}],
            [{"type": "text", "text": "\ud800"}],
        ]
        for blocks in bad:
            with self.subTest(blocks=blocks):
                self.assertEqual((await self.invoke(app, {"prompt": blocks}))[0], 400)
        oversized = [{"type": "resource", "resource": {"uri": "memory://big.txt", "mimeType": "text/plain",
                                                        "text": "x" * 200}}]
        self.assertEqual((await self.invoke(app, {"prompt": oversized}))[0], 413)
        self.assertEqual(app.state.kiro_agentcore.sessions, {})
        self.assertFalse(self.log.exists())

    async def test_remote_permission_is_opt_in_one_time_and_session_scoped(self):
        disabled = self.app()
        self.assertEqual((await self.invoke(disabled, {"action": "interactions"}))[0], 403)
        self.assertEqual((await self.invoke(disabled, {"action": "mcp_reconnect",
            "server_name": "example", "start_oauth": True}))[0], 403)
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "remote-permission", cli_path=str(FAKE),
                                 api_key="synthetic", allow_interactions=True)
        app = self.app(config)
        for decision, expected in (("allow", "yes"), ("deny", "no")):
            turn = asyncio.create_task(self.invoke(app, {"prompt": "permission"}))
            interaction = await self.wait_for_interaction(app)
            self.assertEqual(interaction["kind"], "permission")
            self.assertEqual(interaction["request"]["tool_input"], {"path": "a"})
            ident = interaction["interaction_id"]
            response = {"action": "respond", "interaction_id": ident, "decision": decision}
            self.assertEqual((await self.invoke(app, response, b"other"))[0], 409)
            self.assertEqual((await self.invoke(app, {**response, "persistent": True}))[0], 400)
            self.assertEqual((await self.invoke(app, response))[0], 200)
            self.assertEqual((await self.invoke(app, response))[0], 409)
            status, body, _ = await turn
            self.assertEqual((status, json.loads(body)["response"]), (200, expected))

    async def test_remote_permission_marks_missing_input_without_exposing_other_sessions(self):
        async def authorizer(request, session_id):
            return request.headers.get("x-test-identity") == "owner"

        for missing in (True, False):
            with self.subTest(missing=missing):
                config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / ("missing" if missing else "present"),
                                         cli_path=str(FAKE), api_key="synthetic", allow_interactions=True)
                env = ({"FAKE_PERMISSION_MISSING_INPUT": "1"} if missing else
                       {"FAKE_PERMISSION_RAW_INPUT_JSON": "{}"})
                app = create_agentcore_app(config, options_factory=lambda workspace, resume: KiroAgentOptions(
                    tools=[], env=env), session_authorizer=authorizer)
                self.apps.append(app)
                owner = [(b"x-test-identity", b"owner")]
                other = [(b"x-test-identity", b"other")]
                turn = asyncio.create_task(self.invoke(app, {"prompt": "permission"}, headers=owner))
                try:
                    for _ in range(200):
                        status, body, _ = await self.invoke(app, {"action": "interactions"}, headers=owner)
                        self.assertEqual(status, 200)
                        pending = json.loads(body)["pending"]
                        if pending:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("Permission interaction did not arrive")
                    request_data = pending[0]["request"]
                    self.assertEqual(request_data["tool_input"], {})
                    self.assertEqual(request_data.get("kiro_input_unavailable"), True if missing else None)
                    self.assertEqual((await self.invoke(app, {"action": "interactions"}, headers=other))[0], 403)
                    answer = {"action": "respond", "interaction_id": pending[0]["interaction_id"],
                              "decision": "deny"}
                    self.assertEqual((await self.invoke(app, answer, headers=owner))[0], 200)
                    status, body, _ = await asyncio.wait_for(turn, 10)
                    self.assertEqual((status, json.loads(body)["response"]), (200, "no"))
                finally:
                    if not turn.done():
                        turn.cancel()
                        await asyncio.gather(turn, return_exceptions=True)

    async def test_remote_user_input_and_mcp_elicitation(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "remote-input", cli_path=str(FAKE),
                                 api_key="synthetic", allow_interactions=True)
        app = self.app(config)
        turn = asyncio.create_task(self.invoke(app, {"prompt": "user_input"}))
        interaction = await self.wait_for_interaction(app)
        self.assertEqual((interaction["kind"], interaction["request"]["question"]), ("user_input", "Color?"))
        response = {"action": "respond", "interaction_id": interaction["interaction_id"], "answer": "purple"}
        self.assertEqual((await self.invoke(app, response))[0], 200)
        _, body, _ = await turn
        self.assertEqual(json.loads(json.loads(body)["response"]), {"action": "answered", "answer": "purple"})

        turn = asyncio.create_task(self.invoke(app, {"prompt": "mcp_elicitation"}))
        interaction = await self.wait_for_interaction(app)
        self.assertEqual(interaction["kind"], "mcp_elicitation")
        response = {"action": "respond", "interaction_id": interaction["interaction_id"],
                    "response": {"action": "accept", "content": {"count": 3, "rate": 1.25}}}
        malformed = {"action": "respond", "interaction_id": interaction["interaction_id"],
                     "response": {"action": "accept", "content": {"rate": float("nan")}}}
        self.assertEqual((await self.invoke(app, malformed))[0], 400)
        self.assertEqual((await self.invoke(app, response))[0], 200)
        _, body, _ = await turn
        self.assertEqual(json.loads(json.loads(body)["response"]), response["response"])

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_mcp_oauth_interactions_require_owner_and_are_session_scoped(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "oauth", cli_path=str(FAKE), api_key="synthetic",
                                 allow_interactions=True, allow_mcp_oauth_interactions=True)
        with self.assertRaisesRegex(ValueError, "session_authorizer"):
            create_agentcore_app(config)
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == session_id
        shared = McpOAuthConfig("http://127.0.0.1:9999/callback", None)
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": shared})
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        captured = []
        original_connect = KiroSDKClient.connect
        async def connect_with_oauth(client, *args, **kwargs):
            value = await client.options.mcp_oauth["probe"].authorize("https://example.com/authorize?state=test")
            captured.append(value)
            result = await original_connect(client, *args, **kwargs)
            client._mcp_status_tracker.update("_kiro/mcp/status", {
                "sessionId": client.session_id, "servers": [{"name": "probe", "status": "connected"}]})
            return result
        owner_a = [(b"x-test-identity", b"session-a")]
        owner_b = [(b"x-test-identity", b"session-b")]
        with patch.object(KiroSDKClient, "connect", connect_with_oauth):
            turn = asyncio.create_task(self.invoke(app, {"prompt": "hello"}, headers=owner_a))
            turn_b = asyncio.create_task(self.invoke(app, {"prompt": "hello-b"}, b"session-b", owner_b))
            async def wait(runtime_id, headers):
                while True:
                    status, body, _ = await self.invoke(app, {"action": "interactions"}, runtime_id, headers)
                    self.assertEqual(status, 200)
                    pending = json.loads(body)["pending"]
                    if pending:
                        return pending[0]
                    await asyncio.sleep(0.01)
            item = await asyncio.wait_for(wait(b"session-a", owner_a), 10)
            item_b = await asyncio.wait_for(wait(b"session-b", owner_b), 10)
            self.assertEqual(item["kind"], "mcp_oauth")
            self.assertEqual(item["request"]["server_name"], "probe")
            storage_a = app.state.kiro_agentcore.sessions["session-a"].client.options.mcp_oauth["probe"].storage
            storage_b = app.state.kiro_agentcore.sessions["session-b"].client.options.mcp_oauth["probe"].storage
            self.assertIsNot(storage_a, shared.storage)
            self.assertIsNot(storage_a, storage_b)
            ident = item["interaction_id"]
            answer = {"action": "respond", "interaction_id": ident,
                      "redirect_url": "http://127.0.0.1:9999/callback?code=private&state=test"}
            self.assertEqual((await self.invoke(app, answer, headers=owner_b))[0], 403)
            self.assertEqual((await self.invoke(app, answer, b"session-b", owner_b))[0], 409)
            self.assertEqual((await self.invoke(app, {**answer, "extra": True}, headers=owner_a))[0], 400)
            self.assertEqual((await self.invoke(app, answer, headers=owner_a))[0], 200)
            self.assertEqual((await self.invoke(app, answer, headers=owner_a))[0], 409)
            answer_b = {**answer, "interaction_id": item_b["interaction_id"],
                        "redirect_url": "http://127.0.0.1:9999/callback?code=other&state=test"}
            self.assertEqual((await self.invoke(app, answer_b, b"session-b", owner_b))[0], 200)
            status, body, _ = await turn
            self.assertEqual((status, json.loads(body)["response"]), (200, "hello"))
            status_b, body_b, _ = await turn_b
            self.assertEqual((status_b, json.loads(body_b)["response"]), (200, "hello-b"))
            self.assertCountEqual(captured, [answer["redirect_url"], answer_b["redirect_url"]])
        with self.assertRaisesRegex(ValueError, "allow_interactions"):
            AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, api_key="x", allow_mcp_oauth_interactions=True)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_mcp_oauth_cancellation_does_not_start_a_turn(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "oauth-cancel", cli_path=str(FAKE), api_key="synthetic",
                                 allow_interactions=True, allow_mcp_oauth_interactions=True)
        async def authorizer(req, session_id):
            return True
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        async def connect_with_oauth(client, *args, **kwargs):
            await client.options.mcp_oauth["probe"].authorize("https://example.com/authorize")
        with patch.object(KiroSDKClient, "connect", connect_with_oauth):
            turn = asyncio.create_task(self.invoke(app, {"prompt": "hello"}))
            item = await self.wait_for_interaction(app)
            self.assertEqual(item["kind"], "mcp_oauth")
            answer = {"action": "respond", "interaction_id": item["interaction_id"], "redirect_url": None}
            self.assertEqual((await self.invoke(app, answer))[0], 200)
            self.assert_agent_failure(await turn)
            self.assertEqual((await self.invoke(app, answer))[0], 409)
            self.assertFalse(self.log.exists())

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_idle_oauth_pins_session_and_keeps_owner_isolation(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "oauth-idle", cli_path=str(FAKE), api_key="synthetic",
                                 allow_interactions=True, allow_mcp_oauth_interactions=True, max_sessions=1)
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == session_id
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        original = KiroSDKClient.connect
        async def connect(client, *args, **kwargs):
            result = await original(client, *args, **kwargs)
            client._mcp_status_tracker.update("_kiro/mcp/status", {
                "sessionId": client.session_id, "servers": [{"name": "probe", "status": "connected"}]})
            return result
        owner_a = [(b"x-test-identity", b"session-a")]
        owner_b = [(b"x-test-identity", b"session-b")]
        with patch.object(KiroSDKClient, "connect", connect):
            reconnect = {"action": "mcp_reconnect", "server_name": "probe", "start_oauth": True}
            self.assertEqual((await self.invoke(app, reconnect, headers=owner_a))[0], 409)
            self.assertEqual((await self.invoke(app, {"prompt": "first"}, headers=owner_a))[0], 200)
            session = app.state.kiro_agentcore.sessions["session-a"]
            authorize = session.client.options.mcp_oauth["probe"].authorize
            pending = asyncio.create_task(authorize("https://example.com/authorize?state=test"))
            async def wait():
                while True:
                    status, body, _ = await self.invoke(app, {"action": "interactions"}, headers=owner_a)
                    self.assertEqual(status, 200)
                    items = json.loads(body)["pending"]
                    if items:
                        return items[0]
                    await asyncio.sleep(0.01)
            item = await asyncio.wait_for(wait(), 5)
            self.assertEqual(item["kind"], "mcp_oauth")
            self.assertEqual(session.oauth_pins, 1)
            self.assertEqual((await self.invoke(app, {"prompt": "other"}, headers=owner_a))[0], 409)
            self.assertEqual((await self.invoke(app, {"prompt": "other"}, b"session-b", owner_b))[0], 429)
            answer = {"action": "respond", "interaction_id": item["interaction_id"],
                      "redirect_url": "http://127.0.0.1:9999/callback?code=x&state=test"}
            self.assertEqual((await self.invoke(app, answer, headers=owner_b))[0], 403)
            self.assertEqual((await self.invoke(app, answer, b"session-b", owner_b))[0], 409)
            self.assertEqual((await self.invoke(app, answer, headers=owner_a))[0], 200)
            self.assertEqual(await pending, answer["redirect_url"])
            self.assertEqual(session.oauth_pins, 0)
            self.assertEqual((await self.invoke(app, answer, headers=owner_a))[0], 409)
            self.assertEqual((await self.invoke(app, {"prompt": "next"}, headers=owner_a))[0], 200)
            self.assertEqual((await self.invoke(app, reconnect, headers=owner_b))[0], 403)
            self.assertEqual((await self.invoke(app, {**reconnect, "server_name": "missing"}, headers=owner_a))[0], 400)
            self.assertEqual((await self.invoke(app, {**reconnect, "start_oauth": "true"}, headers=owner_a))[0], 400)
            self.assertEqual((await self.invoke(app, reconnect, headers=owner_a))[0], 200)
            async def fail(*args, **kwargs):
                raise RuntimeError("private-storage-error")
            with patch.object(session.client, "reconnect_mcp_server", fail):
                status, body, _ = await self.invoke(app, reconnect, headers=owner_a)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["is_error"])
            self.assertNotIn(b"private-storage-error", body)
            self.assertIsNone(session.client)
            with self.assertRaises(ValueError):
                await authorize("https://example.com/authorize?state=stale")
            self.assertEqual(session.oauth_pins, 0)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_oauth_survives_end_of_turn_and_blocks_reconnect_until_answered(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "oauth-turn-end", cli_path=str(FAKE), api_key="synthetic",
                                 allow_interactions=True, allow_mcp_oauth_interactions=True)
        async def authorizer(req, session_id):
            return True
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        original = KiroSDKClient.connect
        async def connect(client, *args, **kwargs):
            result = await original(client, *args, **kwargs)
            client._mcp_status_tracker.update("_kiro/mcp/status", {
                "sessionId": client.session_id, "servers": [{"name": "probe", "status": "connected"}]})
            return result
        with patch.object(KiroSDKClient, "connect", connect):
            turn = asyncio.create_task(self.invoke(app, {"prompt": "permission"}))
            permission = await self.wait_for_interaction(app)
            self.assertEqual(permission["kind"], "permission")
            session = app.state.kiro_agentcore.sessions["session-a"]
            authorize = session.client.options.mcp_oauth["probe"].authorize
            pending = asyncio.create_task(authorize("https://example.com/authorize?state=turn"))
            for _ in range(100):
                if session.oauth_pins:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(session.oauth_pins, 1)
            response = {"action": "respond", "interaction_id": permission["interaction_id"], "decision": "allow"}
            self.assertEqual((await self.invoke(app, response))[0], 200)
            self.assertEqual((await turn)[0], 200)
            item = await self.wait_for_interaction(app)
            self.assertEqual(item["kind"], "mcp_oauth")
            reconnect = {"action": "mcp_reconnect", "server_name": "probe", "start_oauth": True}
            self.assertEqual((await self.invoke(app, reconnect))[0], 409)
            answer = {"action": "respond", "interaction_id": item["interaction_id"],
                      "redirect_url": "http://127.0.0.1:9999/callback?code=x&state=turn"}
            self.assertEqual((await self.invoke(app, answer))[0], 200)
            self.assertEqual(await pending, answer["redirect_url"])
            self.assertEqual((await self.invoke(app, reconnect))[0], 200)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_idle_oauth_timeout_and_discard_release_pin(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "oauth-idle-timeout", cli_path=str(FAKE),
                                 api_key="synthetic", allow_interactions=True, allow_mcp_oauth_interactions=True,
                                 interaction_timeout=0.2)
        async def authorizer(req, session_id):
            return True
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        original = KiroSDKClient.connect
        async def connect(client, *args, **kwargs):
            result = await original(client, *args, **kwargs)
            client._mcp_status_tracker.update("_kiro/mcp/status", {
                "sessionId": client.session_id, "servers": [{"name": "probe", "status": "connected"}]})
            return result
        with patch.object(KiroSDKClient, "connect", connect):
            self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
            session = app.state.kiro_agentcore.sessions["session-a"]
            authorize = session.client.options.mcp_oauth["probe"].authorize
            task = asyncio.create_task(authorize("https://example.com/authorize?state=timeout"))
            item = await self.wait_for_interaction(app)
            with self.assertRaises(ValueError):
                await task
            self.assertEqual(session.oauth_pins, 0)
            self.assertEqual((await self.invoke(app, {"action": "respond",
                "interaction_id": item["interaction_id"], "redirect_url": None}))[0], 409)
            task = asyncio.create_task(authorize("https://example.com/authorize?state=discard"))
            await self.wait_for_interaction(app)
            await asyncio.wait_for(app.state.kiro_agentcore._discard_client(session), 5)
            with self.assertRaises(ValueError):
                await task
            self.assertEqual(session.oauth_pins, 0)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_mcp_reconnect_timeout_discards_uncertain_client(self):
        from dataclasses import replace
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "oauth-reconnect-timeout", cli_path=str(FAKE),
                                 api_key="synthetic", allow_interactions=True, allow_mcp_oauth_interactions=True)
        async def authorizer(req, session_id):
            return True
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        original = KiroSDKClient.connect
        async def connect(client, *args, **kwargs):
            result = await original(client, *args, **kwargs)
            client._mcp_status_tracker.update("_kiro/mcp/status", {
                "sessionId": client.session_id, "servers": [{"name": "probe", "status": "connected"}]})
            return result
        with patch.object(KiroSDKClient, "connect", connect):
            self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
            service = app.state.kiro_agentcore
            session = service.sessions["session-a"]
            client = session.client
            async def hang(*args, **kwargs):
                await asyncio.Event().wait()
            service.config = replace(config, startup_timeout=0.2)
            with patch.object(client, "reconnect_mcp_server", hang):
                result = await self.invoke(app, {"action": "mcp_reconnect", "server_name": "probe",
                                                 "start_oauth": True})
            self.assert_agent_failure(result)
            self.assertIsNone(session.client)
            self.assertFalse(session.lock.locked())
            service.config = config
            self.assertEqual((await self.invoke(app, {"prompt": "next"}))[0], 200)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_oauth_callback_cannot_deadlock_or_pin_a_session_being_evicted(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "oauth-eviction", cli_path=str(FAKE), api_key="synthetic",
                                 allow_interactions=True, allow_mcp_oauth_interactions=True, max_sessions=1)
        async def authorizer(req, session_id):
            return True
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        original_connect = KiroSDKClient.connect
        async def connect(client, *args, **kwargs):
            result = await original_connect(client, *args, **kwargs)
            client._mcp_status_tracker.update("_kiro/mcp/status", {
                "sessionId": client.session_id, "servers": [{"name": "probe", "status": "connected"}]})
            return result
        with patch.object(KiroSDKClient, "connect", connect):
            self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
            session = app.state.kiro_agentcore.sessions["session-a"]
            client = session.client
            authorize = client.options.mcp_oauth["probe"].authorize
            started, release = asyncio.Event(), asyncio.Event()
            original_disconnect = client.disconnect
            async def disconnect():
                started.set()
                await release.wait()
                await original_disconnect()
            with patch.object(client, "disconnect", disconnect):
                replacement = asyncio.create_task(self.invoke(app, {"prompt": "second"}, b"session-b"))
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    with self.assertRaises(ValueError):
                        await asyncio.wait_for(authorize("https://example.com/authorize?state=stale"), 1)
                    self.assertEqual(session.oauth_pins, 0)
                finally:
                    release.set()
                self.assertEqual((await asyncio.wait_for(replacement, 5))[0], 200)
            self.assertNotIn("session-a", app.state.kiro_agentcore.sessions)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_oauth_pin_is_rechecked_after_delayed_session_lock(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "oauth-lock-race", cli_path=str(FAKE), api_key="synthetic",
                                 allow_interactions=True, allow_mcp_oauth_interactions=True, max_sessions=1)
        async def authorizer(req, session_id):
            return True
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", None)})
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        original_connect = KiroSDKClient.connect
        async def connect(client, *args, **kwargs):
            result = await original_connect(client, *args, **kwargs)
            client._mcp_status_tracker.update("_kiro/mcp/status", {
                "sessionId": client.session_id, "servers": [{"name": "probe", "status": "connected"}]})
            return result
        class DelayedLock:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.started, self.resume = asyncio.Event(), asyncio.Event()
            def locked(self):
                return self.lock.locked()
            async def acquire(self):
                self.started.set()
                await self.resume.wait()
                return await self.lock.acquire()
            def release(self):
                self.lock.release()
        with patch.object(KiroSDKClient, "connect", connect):
            self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
            session = app.state.kiro_agentcore.sessions["session-a"]
            authorize = session.client.options.mcp_oauth["probe"].authorize
            for mode in ("eviction", "turn", "reconnect"):
                with self.subTest(mode=mode):
                    lock = DelayedLock()
                    session.lock = lock
                    if mode == "eviction":
                        attempt = asyncio.create_task(self.invoke(app, {"prompt": "other"}, b"session-b"))
                    elif mode == "turn":
                        attempt = asyncio.create_task(self.invoke(app, {"prompt": "other"}))
                    else:
                        attempt = asyncio.create_task(self.invoke(app, {"action": "mcp_reconnect",
                            "server_name": "probe", "start_oauth": True}))
                    await asyncio.wait_for(lock.started.wait(), 5)
                    pending = asyncio.create_task(authorize("https://example.com/authorize?state=race"))
                    for _ in range(100):
                        if session.oauth_pins:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(session.oauth_pins, 1)
                    lock.resume.set()
                    self.assertEqual((await attempt)[0], 429 if mode == "eviction" else 409)
                    item = await self.wait_for_interaction(app)
                    answer = {"action": "respond", "interaction_id": item["interaction_id"],
                              "redirect_url": "http://127.0.0.1:9999/callback?code=x&state=race"}
                    self.assertEqual((await self.invoke(app, answer))[0], 200)
                    self.assertEqual(await pending, answer["redirect_url"])
                    self.assertEqual(session.oauth_pins, 0)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_mcp_reconnect_static_remote_without_starting_oauth(self):
        async def authorizer(req, session_id):
            return True
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp",
                                         "headers": {"Authorization": "Bearer local-only"}}})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
        payload = {"action": "mcp_reconnect", "server_name": "probe", "start_oauth": True}
        self.assertEqual((await self.invoke(app, payload))[0], 400)
        payload["start_oauth"] = False
        status, body, _ = await self.invoke(app, payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "requested")
        self.assertNotIn(b"local-only", body)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_mcp_oauth_revocation_is_session_scoped_and_disconnects(self):
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner" and session_id == "session-a"
        async def authorize(url):
            return url
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", authorize)})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        self.assertEqual((await self.invoke(app, {"prompt": "first"}, headers=owner))[0], 200)
        session = app.state.kiro_agentcore.sessions["session-a"]
        action = {"action": "mcp_revoke_oauth", "server_name": "probe"}
        self.assertEqual((await self.invoke(app, action, headers=[(b"x-test-identity", b"other")]))[0], 403)
        self.assertEqual((await self.invoke(app, {**action, "server_name": "missing"}, headers=owner))[0], 400)
        await session.lock.acquire()
        try:
            self.assertEqual((await self.invoke(app, action, headers=owner))[0], 409)
        finally:
            session.lock.release()
        async def revoke(name):
            self.assertEqual(name, "probe")
            return {"access_token": True, "refresh_token": True}
        with patch.object(session.client, "revoke_mcp_oauth_tokens", revoke):
            status, body, _ = await self.invoke(app, action, headers=owner)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"action": "mcp_revoke_oauth", "status": "requested",
                                           "tokens": {"access_token": True, "refresh_token": True}})
        self.assertIsNone(session.client)
        self.assertEqual((await self.invoke(app, {"prompt": "next"}, headers=owner))[0], 200)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_mcp_oauth_revocation_failure_discards_client_without_leaking(self):
        async def authorizer(req, session_id):
            return True
        async def authorize(url):
            return url
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", authorize)})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
        session = app.state.kiro_agentcore.sessions["session-a"]
        async def fail(name):
            raise RuntimeError("private-token")
        with patch.object(session.client, "revoke_mcp_oauth_tokens", fail):
            result = await self.invoke(app, {"action": "mcp_revoke_oauth", "server_name": "probe"})
        self.assert_agent_failure(result)
        self.assertNotIn(b"private-token", result[1])
        self.assertIsNone(session.client)
        self.assertFalse(session.lock.locked())

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_mcp_oauth_revocation_timeout_discards_uncertain_client(self):
        from dataclasses import replace
        async def authorizer(req, session_id):
            return True
        async def authorize(url):
            return url
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": "https://example.com/mcp"}},
                mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", authorize)})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
        service = app.state.kiro_agentcore
        session = service.sessions["session-a"]
        async def hang(name):
            await asyncio.Event().wait()
        service.config = replace(self.config, startup_timeout=0.2)
        with patch.object(session.client, "revoke_mcp_oauth_tokens", hang):
            result = await self.invoke(app, {"action": "mcp_revoke_oauth", "server_name": "probe"})
        self.assert_agent_failure(result)
        self.assertIsNone(session.client)
        self.assertFalse(session.lock.locked())

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_mcp_oauth_revocation_reaches_the_server(self):
        from test_mcp_oauth import ClientSession, OAuthFixture

        fixture = OAuthFixture()
        url = await fixture.start()
        auth = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)
        async def authorizer(req, session_id):
            return session_id == "session-a"
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "http", "url": url}}, mcp_oauth={"probe": auth})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        try:
            self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
            session = app.state.kiro_agentcore.sessions["session-a"]
            bridge = session.client._named_external_mcp_bridges["probe"][0]
            async with bridge._remote_streams() as (reader, writer):
                async with ClientSession(reader, writer) as remote:
                    await remote.initialize()
            status, body, _ = await self.invoke(app, {"action": "mcp_revoke_oauth", "server_name": "probe"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["tokens"], {"access_token": True, "refresh_token": True})
            self.assertEqual(len(fixture.revocations), 2)
            self.assertIsNone(await auth.storage.get_tokens())
            self.assertIsNone(session.client)
        finally:
            await fixture.close()

    async def test_remote_interaction_timeout_denies_and_cannot_be_replayed(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "remote-timeout", cli_path=str(FAKE),
                                 api_key="synthetic", allow_interactions=True, interaction_timeout=0.3)
        app = self.app(config)
        turn = asyncio.create_task(self.invoke(app, {"prompt": "permission"}))
        interaction = await self.wait_for_interaction(app)
        status, body, _ = await turn
        self.assertEqual((status, json.loads(body)["response"]), (200, "no"))
        response = {"action": "respond", "interaction_id": interaction["interaction_id"], "decision": "allow"}
        self.assertEqual((await self.invoke(app, response))[0], 409)
        for prompt, expected in (("user_input", {"action": "dismissed"}),
                                 ("mcp_elicitation", {"action": "cancel"})):
            status, body, _ = await self.invoke(app, {"prompt": prompt})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(json.loads(body)["response"]), expected)

    async def test_cancelling_turn_removes_remote_interactions(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "remote-cancel", cli_path=str(FAKE),
                                 api_key="synthetic", allow_interactions=True)
        app = self.app(config)
        turn = asyncio.create_task(self.invoke(app, {"prompt": "permission"}))
        interaction = await self.wait_for_interaction(app)
        turn.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await turn
        response = {"action": "respond", "interaction_id": interaction["interaction_id"], "decision": "allow"}
        self.assertEqual((await self.invoke(app, response))[0], 409)
        self.assertEqual((await self.invoke(app, {"prompt": "next"}))[0], 200)

    async def test_finished_turn_removes_unanswered_remote_interactions(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "remote-finished", cli_path=str(FAKE),
                                 api_key="synthetic", allow_interactions=True)
        app = self.app(config)
        turn = asyncio.create_task(self.invoke(app, {"prompt": "permission_abandoned"}))
        interaction = await self.wait_for_interaction(app)
        session = app.state.kiro_agentcore.sessions["session-a"]
        await session.client.extension_request("_kiro/testFinish", {})
        status, body, _ = await turn
        self.assertEqual((status, json.loads(body)["response"]), (200, "finished"))
        response = {"action": "respond", "interaction_id": interaction["interaction_id"], "decision": "allow"}
        self.assertEqual((await self.invoke(app, response))[0], 409)
        self.assertEqual((await self.invoke(app, {"prompt": "next"}))[0], 200)

    async def test_remote_interaction_queue_has_count_and_size_limits(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "remote-limits", cli_path=str(FAKE),
                                 api_key="synthetic", allow_interactions=True)
        app = self.app(config)
        turn = asyncio.create_task(self.invoke(app, {"prompt": "permission"}))
        await self.wait_for_interaction(app)
        service = app.state.kiro_agentcore
        session = service.sessions["session-a"]
        extra = [asyncio.create_task(service._await_interaction(session, "user_input", {"question": "?"}))
                 for _ in range(15)]
        await asyncio.sleep(0)
        self.assertEqual(len(session.interactions), 16)
        self.assertIsNone(await service._await_interaction(session, "user_input", {"question": "overflow"}))
        for task in extra:
            task.cancel()
        await asyncio.gather(*extra, return_exceptions=True)
        self.assertIsNone(await service._await_interaction(session, "user_input", {"question": "x" * (256 * 1024)}))
        turn.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await turn

    async def test_remote_interactions_reject_existing_callbacks_or_permission_modes(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "remote-conflict", cli_path=str(FAKE),
                                 api_key="synthetic", allow_interactions=True)
        async def callback(*args):
            return None
        for options in (KiroAgentOptions(tools=[], can_use_tool=callback),
                        KiroAgentOptions(tools=[], on_user_input=callback),
                        KiroAgentOptions(tools=[], permission_mode="trustAll")):
            with self.subTest(options=options):
                app = self.app(config, factory=lambda workspace, resume: options)
                with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
                    self.assert_agent_failure(await self.invoke(app, {"prompt": "hello"}))

    async def test_busy_session_and_cancellation_release_lease(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hook(event, tool_id, context):
            entered.set()
            await release.wait()
            return {}

        app = self.app(factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], hooks={"promptSubmit": [HookMatcher(hooks=[hook])]}))
        first = asyncio.create_task(self.invoke(app, {"prompt": "first"}))
        await asyncio.wait_for(entered.wait(), 10)
        status, body, _ = await request(app, "GET", "/ping")
        self.assertEqual((status, json.loads(body)["status"]), (200, "HealthyBusy"))
        status, _, _ = await self.invoke(app, {"prompt": "other"})
        self.assertEqual(status, 409)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        status, body, _ = await self.invoke(app, {"prompt": "next"})
        self.assertEqual((status, json.loads(body)["response"]), (200, "next"))
        status, body, _ = await request(app, "GET", "/ping")
        self.assertEqual((status, json.loads(body)["status"]), (200, "Healthy"))

    async def test_stream_disconnect_does_not_leave_session_busy(self):
        app = self.app()
        incoming = [{"type": "http.request", "body": b'{"prompt":"cancel","stream":true}',
                     "more_body": False}]
        started = asyncio.Event()

        async def receive():
            if incoming:
                return incoming.pop(0)
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                started.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/invocations",
                 "raw_path": b"/invocations", "query_string": b"", "root_path": "",
                 "headers": [(HEADER, b"session-a")], "server": ("localhost", 8080),
                 "client": ("localhost", 1)}
        await asyncio.wait_for(app(scope, receive, send), 15)
        status, body, _ = await self.invoke(app, {"prompt": "next"})
        self.assertEqual((status, json.loads(body)["response"]), (200, "next"))

    async def test_interrupt_active_turn_via_http(self):
        app = self.app()
        first = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}))
        await self.wait_for_active_turn(app)
        status, body, _ = await self.invoke(app, {"action": "interrupt"})
        self.assertEqual((status, json.loads(body)), (200, {"action": "interrupt", "status": "requested"}))
        status, body, _ = await first
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertEqual(json.loads(body)["stop_reason"], "cancelled")
        self.assertEqual((await self.invoke(app, {"action": "interrupt"}))[0], 409)
        self.assertEqual((await self.invoke(app, {"prompt": "next"}))[0], 200)

    async def test_cancel_managed_mcp_tools_during_active_turn_via_http(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        @tool("probe", "Wait for cancellation", {})
        async def probe(args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        app = self.app(factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)},
            mcp_servers={"example": create_mcp_server("example", tools=[probe])}))
        turn = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}))
        session = await self.wait_for_active_turn(app)
        bridge = session.client._mcp_servers[0]
        bridge_env = {item["name"]: item["value"] for item in bridge["env"]}
        reader, writer = await asyncio.open_connection("127.0.0.1", int(bridge_env["KIRO_SDK_MCP_PORT"]))
        try:
            request_data = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "probe", "arguments": {}}}
            writer.write((bridge_env["KIRO_SDK_MCP_TOKEN"] + "\n" + json.dumps(request_data) + "\n").encode())
            await writer.drain()
            await asyncio.wait_for(started.wait(), 5)
            self.assertEqual((await self.invoke(app, {"action": "mcp_cancel_tools", "server_name": "unknown"}))[0], 400)
            status, body, _ = await self.invoke(app, {"action": "mcp_cancel_tools", "server_name": "example"})
            self.assertEqual((status, json.loads(body)), (200, {"action": "mcp_cancel_tools", "status": "requested"}))
            self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 5))["error"]["code"], -32800)
            await asyncio.wait_for(cancelled.wait(), 5)
            self.assertFalse(turn.done())
            self.assertEqual((await self.invoke(app, {"action": "interrupt"}))[0], 200)
            self.assertEqual((await turn)[0], 200)
            self.assertEqual((await self.invoke(app, {"action": "mcp_cancel_tools", "server_name": "example"}))[0], 409)
        finally:
            writer.close()
            await writer.wait_closed()
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_sse_cancel_token_in_authorized_agentcore_session(self):
        from test_mcp_oauth import OAuthFixture
        fixture = OAuthFixture("sse")
        fixture.tool_call_received = asyncio.Event()
        fixture.tool_call_release = asyncio.Event()
        url = await fixture.start()
        calls = []
        async def replacement():
            calls.append(True)
            return "renewed-secret"
        auth = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize,
                              sse_cancel_token_provider=replacement)
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"
        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "sse", "url": url}}, mcp_oauth={"probe": auth})
        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        turn = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}, headers=owner))
        writer = None
        try:
            session = await self.wait_for_active_turn(app)
            env = {item["name"]: item["value"] for item in session.client._mcp_servers[0]["env"]}
            reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
            writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
            async def send(message):
                writer.write((json.dumps(message) + "\n").encode())
                await writer.drain()
            await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"}}})
            self.assertIn("result", json.loads(await asyncio.wait_for(reader.readline(), 8)))
            await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await send({"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                        "params": {"name": "probe", "arguments": {}}})
            await asyncio.wait_for(fixture.tool_call_received.wait(), 8)
            fixture.cancel_expected_token = "renewed-secret"
            action = {"action": "mcp_cancel_tools", "server_name": "probe"}
            self.assertEqual((await self.invoke(app, action,
                                               headers=[(b"x-test-identity", b"other")]))[0], 403)
            status, body, _ = await self.invoke(app, action, headers=owner)
            self.assertEqual((status, json.loads(body)["status"]), (200, "requested"))
            self.assertNotIn("renewed-secret", body.decode())
            self.assertEqual((fixture.cancellations, calls), ([41], [True]))
            self.assertEqual((await self.invoke(app, {"action": "interrupt"}, headers=owner))[0], 200)
            await asyncio.wait_for(turn, 8)
        finally:
            fixture.tool_call_release.set()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            await fixture.close()

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
    async def test_sse_cancel_stored_token_in_authorized_agentcore_session(self):
        from mcp.shared.auth import OAuthToken
        from kiro_agent_sdk import InMemoryMcpOAuthStorage
        from test_mcp_oauth import OAuthFixture

        fixture = OAuthFixture("sse")
        fixture.tool_call_received = asyncio.Event()
        fixture.tool_call_release = asyncio.Event()
        url = await fixture.start()
        storage = InMemoryMcpOAuthStorage()
        auth = McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize,
                              storage=storage, sse_cancel_retry_with_stored_token=True)

        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"

        def factory(workspace, resume):
            return KiroAgentOptions(tools=[], sdk_mcp_toggle_mode="sdk-all",
                mcp_servers={"probe": {"type": "sse", "url": url}}, mcp_oauth={"probe": auth})

        app = create_agentcore_app(self.config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        owner = [(b"x-test-identity", b"owner")]
        turn = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}, headers=owner))
        writer = None
        try:
            session = await self.wait_for_active_turn(app)
            env = {item["name"]: item["value"] for item in session.client._mcp_servers[0]["env"]}
            reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
            writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())

            async def send(message):
                writer.write((json.dumps(message) + "\n").encode())
                await writer.drain()

            await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"}}})
            self.assertIn("result", json.loads(await asyncio.wait_for(reader.readline(), 8)))
            await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await send({"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                        "params": {"name": "probe", "arguments": {}}})
            await asyncio.wait_for(fixture.tool_call_received.wait(), 8)
            await storage.set_tokens(OAuthToken(access_token="renewed-secret", token_type="Bearer"))
            fixture.cancel_expected_token = "renewed-secret"
            action = {"action": "mcp_cancel_tools", "server_name": "probe"}
            self.assertEqual((await self.invoke(app, action,
                                               headers=[(b"x-test-identity", b"other")]))[0], 403)
            status, body, _ = await self.invoke(app, action, headers=owner)
            self.assertEqual((status, json.loads(body)["status"]), (200, "requested"))
            self.assertNotIn("renewed-secret", body.decode())
            self.assertEqual(fixture.cancellations, [41])
            self.assertEqual(fixture.token_grants, ["authorization_code"])
            self.assertEqual((await self.invoke(app, {"action": "interrupt"}, headers=owner))[0], 200)
            await asyncio.wait_for(turn, 8)
        finally:
            fixture.tool_call_release.set()
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            if not turn.done():
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            await fixture.close()

    async def test_steer_active_turn_via_http(self):
        app = self.app()
        first = asyncio.create_task(self.invoke(app, {"prompt": "steer"}))
        await self.wait_for_active_turn(app)
        status, body, _ = await self.invoke(app, {"action": "steer", "message": "new instruction"})
        self.assertEqual((status, json.loads(body)), (200, {"action": "steer", "status": "requested"}))
        status, body, _ = await first
        self.assertEqual((status, json.loads(body)["response"]), (200, "new instruction"))
        self.assertEqual((await self.invoke(app, {"prompt": "next"}))[0], 200)
        methods = [json.loads(line)["method"] for line in self.log.read_text().splitlines()]
        self.assertLess(methods.index("_session/steer/clear"),
                        len(methods) - 1 - methods[::-1].index("session/prompt"))

    async def test_unconfirmed_steering_clear_prevents_next_prompt(self):
        app = self.app(factory=lambda workspace, resume: KiroAgentOptions(tools=[], env={
            "FAKE_LOG_REQUESTS": str(self.log), "FAKE_STEER_CLEAR_FAIL": "1"}))
        first = asyncio.create_task(self.invoke(app, {"prompt": "steer"}))
        await self.wait_for_active_turn(app)
        self.assertEqual((await self.invoke(app, {"action": "steer", "message": "instruction"}))[0], 200)
        self.assertEqual((await first)[0], 200)
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
            self.assert_agent_failure(await self.invoke(app, {"prompt": "must-not-run"}))
        methods = [json.loads(line)["method"] for line in self.log.read_text().splitlines()]
        self.assertEqual(methods.count("session/prompt"), 1)
        self.assertTrue(app.state.kiro_agentcore.sessions["session-a"].clear_steering)

    async def test_pending_steering_is_cleared_after_eviction_and_restart(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "steering-state", cli_path=str(FAKE),
                                 api_key="synthetic", max_sessions=1)
        app = self.app(config)
        first = asyncio.create_task(self.invoke(app, {"prompt": "steer"}, b"one"))
        session = await self.wait_for_active_turn(app, "one")
        self.assertEqual((await self.invoke(app, {"action": "steer", "message": "instruction"}, b"one"))[0], 200)
        self.assertEqual((await first)[0], 200)
        marker = session.directory / "session.json"
        self.assertTrue(json.loads(marker.read_text())["steering_pending"])
        self.assertEqual((await self.invoke(app, {"prompt": "other"}, b"two"))[0], 200)
        await app.state.kiro_agentcore.close()
        restarted = self.app(config)
        self.assertEqual((await self.invoke(restarted, {"prompt": "next"}, b"one"))[0], 200)
        self.assertFalse(json.loads(marker.read_text())["steering_pending"])
        methods = [json.loads(line)["method"] for line in self.log.read_text().splitlines()]
        self.assertIn("session/load", methods)
        self.assertLess(methods.index("_session/steer/clear"),
                        len(methods) - 1 - methods[::-1].index("session/prompt"))

    async def test_invalid_control_does_not_create_session(self):
        app = self.app()
        self.assertEqual((await self.invoke(app, {"action": "interrupt"}))[0], 409)
        for payload in ({"action": "interrupt", "prompt": "not allowed"},
                        {"action": "steer", "message": ""},
                        {"action": "steer", "message": "x\0y"},
                        {"action": "steer", "message": ["not a string"]},
                        {"action": "mcp_cancel_tools", "server_name": ""},
                        {"action": "mcp_cancel_tools", "server_name": "bad\0name"},
                        {"action": "mcp_cancel_tools", "server_name": "x" * 257},
                        {"action": "mcp_cancel_tools", "server_name": ["not a string"]},
                        {"action": "mcp_cancel_tools", "server_name": "example", "extra": True},
                        {"action": "other"}):
            with self.subTest(payload=payload):
                self.assertEqual((await self.invoke(app, payload))[0], 400)
        self.assertEqual(app.state.kiro_agentcore.sessions, {})
        v2 = self.app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "v2", cli_path=str(FAKE),
                                      api_key="synthetic", agent_engine="v2"))
        self.assertEqual((await self.invoke(v2, {"action": "steer", "message": "hello"}))[0], 400)

    async def test_pending_control_pins_client_after_turn_is_cancelled(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "pinned", cli_path=str(FAKE),
                                 api_key="synthetic", max_sessions=1)
        app = self.app(config)
        first = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}, b"one"))
        session = await self.wait_for_active_turn(app, "one")
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_steer(message):
            entered.set()
            await release.wait()
            return {"queued": True}

        session.client.steer = delayed_steer
        control = asyncio.create_task(self.invoke(app, {"action": "steer", "message": "later"}, b"one"))
        await asyncio.wait_for(entered.wait(), 10)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual((await self.invoke(app, {"prompt": "other"}, b"two"))[0], 429)
        self.assertEqual((await self.invoke(app, {"prompt": "same"}, b"one"))[0], 409)
        release.set()
        self.assertEqual((await control)[0], 200)
        self.assertEqual((await self.invoke(app, {"prompt": "other"}, b"two"))[0], 200)

    async def test_failed_control_discards_client_before_next_turn(self):
        app = self.app()
        first = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}))
        session = await self.wait_for_active_turn(app)
        old_client = session.client

        async def fail(message):
            raise RuntimeError("synthetic-test-key")

        old_client.steer = fail
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
            status, body, _ = await self.invoke(app, {"action": "steer", "message": "new"})
        self.assert_agent_failure((status, body, {}))
        self.assertNotIn("synthetic-test-key", body.decode() + str(logs.output))
        await first
        self.assertFalse(old_client._connected)
        self.assertEqual((await self.invoke(app, {"prompt": "next"}))[0], 200)

    async def test_unconfirmed_steering_is_not_reported_as_success(self):
        app = self.app()
        first = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}))
        session = await self.wait_for_active_turn(app)
        old_client = session.client

        async def unconfirmed(message):
            return {}

        old_client.steer = unconfirmed
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
            self.assert_agent_failure(await self.invoke(app, {"action": "steer", "message": "new"}))
        self.assert_agent_failure(await first)
        self.assertFalse(old_client._connected)
        self.assertEqual((await self.invoke(app, {"prompt": "next"}))[0], 200)

    async def test_cancelled_control_discards_client_before_next_turn(self):
        app = self.app()
        first = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}))
        session = await self.wait_for_active_turn(app)
        old_client, entered = session.client, asyncio.Event()

        async def wait_forever(message):
            entered.set()
            await asyncio.Event().wait()

        old_client.steer = wait_forever
        control = asyncio.create_task(self.invoke(app, {"action": "steer", "message": "new"}))
        await asyncio.wait_for(entered.wait(), 10)
        control.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await control
        self.assert_agent_failure(await first)
        self.assertFalse(old_client._connected)
        self.assertEqual((await self.invoke(app, {"prompt": "next"}))[0], 200)

    async def test_error_does_not_disclose_secret(self):
        bad = self.app(factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], env={"FAKE_BAD_JSON": "1"}))
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
            status, body, _ = await self.invoke(bad, {"prompt": "hello"})
        self.assert_agent_failure((status, body, {}))
        self.assertNotIn("synthetic-test-key", body.decode() + str(logs.output))

    async def test_capacity_evicts_idle_client_and_resumes_its_session(self):
        limited = self.app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "capacity", cli_path=str(FAKE),
                                           api_key="synthetic", max_sessions=1))
        self.assertEqual((await self.invoke(limited, {"prompt": "one"}, b"one"))[0], 200)
        service = limited.state.kiro_agentcore
        old = service.sessions["one"]
        old_client = old.client
        self.assertEqual((await self.invoke(limited, {"prompt": "two"}, b"two"))[0], 200)
        self.assertIsNone(old.client)
        self.assertFalse(old_client._connected)
        self.assertTrue((old.directory / "session.json").is_file())
        self.assertEqual(set(service.sessions), {"two"})
        status, body, _ = await self.invoke(limited, {"prompt": "again"}, b"one")
        self.assertEqual((status, json.loads(body)["response"]), (200, "again"))
        entries = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(any(entry["method"] == "session/load" for entry in entries))

    async def test_capacity_does_not_evict_active_sessions(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hook(event, tool_id, context):
            entered.set()
            await release.wait()
            return {}

        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "busy", cli_path=str(FAKE),
                                 api_key="synthetic", max_sessions=1)
        app = self.app(config, factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], hooks={"promptSubmit": [HookMatcher(hooks=[hook])]}))
        first = asyncio.create_task(self.invoke(app, {"prompt": "first"}, b"one"))
        await asyncio.wait_for(entered.wait(), 10)
        self.assertEqual((await self.invoke(app, {"prompt": "second"}, b"two"))[0], 429)
        release.set()
        self.assertEqual((await first)[0], 200)
        self.assertEqual((await self.invoke(app, {"prompt": "second"}, b"two"))[0], 200)

    async def test_capacity_evicts_least_recently_used_idle_session(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "lru", cli_path=str(FAKE),
                                 api_key="synthetic", max_sessions=2)
        app = self.app(config)
        for runtime_id in (b"one", b"two", b"one", b"three"):
            self.assertEqual((await self.invoke(app, {"prompt": "hello"}, runtime_id))[0], 200)
        self.assertEqual(set(app.state.kiro_agentcore.sessions), {"one", "three"})

    async def test_eviction_failure_keeps_client_registered(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "failure", cli_path=str(FAKE),
                                 api_key="synthetic", max_sessions=1)
        app = self.app(config)
        self.assertEqual((await self.invoke(app, {"prompt": "one"}, b"one"))[0], 200)
        service = app.state.kiro_agentcore
        original = service.sessions["one"].client.disconnect

        async def fail():
            raise RuntimeError("synthetic-secret")

        service.sessions["one"].client.disconnect = fail
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
            self.assert_agent_failure(await self.invoke(app, {"prompt": "two"}, b"two"))
        self.assertNotIn("synthetic-secret", str(logs.output))
        self.assertEqual(set(service.sessions), {"one"})
        self.assertTrue(service.sessions["one"].broken)
        self.assertFalse(service.sessions["one"].lock.locked())
        service.sessions["one"].client.disconnect = original
        self.assertEqual((await self.invoke(app, {"prompt": "two"}, b"two"))[0], 200)

    async def test_cancelled_eviction_keeps_client_registered(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "cancel-eviction", cli_path=str(FAKE),
                                 api_key="synthetic", max_sessions=1)
        app = self.app(config)
        self.assertEqual((await self.invoke(app, {"prompt": "one"}, b"one"))[0], 200)
        service = app.state.kiro_agentcore
        session = service.sessions["one"]
        original, entered = session.client.disconnect, asyncio.Event()

        async def wait_for_cancellation():
            entered.set()
            await asyncio.Event().wait()

        session.client.disconnect = wait_for_cancellation
        request_task = asyncio.create_task(self.invoke(app, {"prompt": "two"}, b"two"))
        await asyncio.wait_for(entered.wait(), 10)
        request_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request_task
        self.assertEqual(set(service.sessions), {"one"})
        self.assertTrue(session.broken)
        self.assertFalse(session.lock.locked())
        session.client.disconnect = original
        self.assertEqual((await self.invoke(app, {"prompt": "two"}, b"two"))[0], 200)

    async def test_corrupt_marker_fails_without_starting_a_new_session(self):
        app = self.app()
        self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
        marker = app.state.kiro_agentcore.sessions["session-a"].directory / "session.json"
        await app.state.kiro_agentcore.close()
        marker.write_text('{"engine":"v2","session_id":"wrong"}')
        before = self.log.read_text()
        resumed = self.app()
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
            result = await self.invoke(resumed, {"prompt": "must-not-run"})
        self.assert_agent_failure(result)
        self.assertEqual(self.log.read_text(), before)

    async def test_options_factory_cannot_override_session_or_authentication(self):
        app = self.app(factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], env={"HOME": "/incorrect", "KIRO_API_KEY": "incorrect"}))
        self.assertEqual((await self.invoke(app, {"prompt": "hello"}))[0], 200)
        session = app.state.kiro_agentcore.sessions["session-a"]
        self.assertEqual(session.client.options.env["HOME"], str(session.directory / "home"))
        self.assertEqual(session.client.options.env["KIRO_API_KEY"], "synthetic-test-key")
        bad = self.app(factory=lambda workspace, resume: KiroAgentOptions(tools=[], resume="other"))
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING"):
            self.assert_agent_failure(await self.invoke(bad, {"prompt": "hello"}))

    async def test_failed_cleanup_cannot_reuse_client_or_log_exception_details(self):
        app = self.app()
        self.assertEqual((await self.invoke(app, {"prompt": "first"}))[0], 200)
        service = app.state.kiro_agentcore
        session = service.sessions["session-a"]
        client = session.client
        original = client.disconnect

        async def fail():
            raise RuntimeError("synthetic-test-key")

        client.disconnect = fail
        with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await service._discard_client(session)
        self.assertNotIn("synthetic-test-key", str(logs.output))
        self.assertTrue(session.broken)
        self.assertIs(session.client, client)
        client.disconnect = original
        status, body, _ = await self.invoke(app, {"prompt": "second"})
        self.assertEqual((status, json.loads(body)["response"]), (200, "second"))
        self.assertFalse(session.broken)
        self.assertIsNot(session.client, client)

    async def test_failed_connect_cleanup_does_not_start_another_client(self):
        created = []
        class BrokenClient:
            def __init__(self, options):
                created.append(self)
            async def connect(self):
                raise ValueError("synthetic-test-key")
            async def disconnect(self):
                raise RuntimeError("synthetic-test-key")
        app = self.app()
        with patch("kiro_agent_sdk.agentcore.KiroSDKClient", BrokenClient):
            with self.assertLogs("kiro_agent_sdk.agentcore", level="WARNING") as logs:
                self.assert_agent_failure(await self.invoke(app, {"prompt": "one"}))
                self.assert_agent_failure(await self.invoke(app, {"prompt": "two"}))
            self.assertNotIn("synthetic-test-key", str(logs.output))
            self.assertEqual(len(created), 1)
            self.assertTrue(app.state.kiro_agentcore.sessions["session-a"].broken)
            async def cleaned():
                pass
            created[0].disconnect = cleaned

    async def test_error_result_disconnects_before_next_turn(self):
        app = self.app(factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], env={"FAKE_STOP_REASON": "cancelled", "FAKE_LOG_REQUESTS": str(self.log)}))
        status, body, _ = await self.invoke(app, {"prompt": "first"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertIsNone(app.state.kiro_agentcore.sessions["session-a"].client)
        status, body, _ = await self.invoke(app, {"prompt": "second", "stream": True})
        self.assertEqual(status, 200)
        self.assertTrue(any(json.loads(line[6:]).get("is_error")
                            for line in body.splitlines() if line.startswith(b"data: ")))
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(any(item["method"] == "session/load" for item in requests))

    async def test_secrets_manager_and_public_runtime_cache(self):
        sessions = []
        def make_session():
            sessions.append(object())
            return types.SimpleNamespace(client=lambda name, **kwargs: types.SimpleNamespace(
                get_secret_value=lambda **kwargs: {"SecretString": '{"kiro":"value"}'}))
        fake = types.SimpleNamespace(Session=make_session)
        with patch.dict("sys.modules", {"boto3": fake}):
            self.assertEqual(_load_secret("test", "kiro"), "value")
            with self.assertRaises(ValueError):
                _load_secret("test", "missing")
        self.assertEqual(len(sessions), 2)
        source = self.root / "cache"
        (source / "kas" / "bundle").mkdir(parents=True)
        (source / "kas" / "bundle" / "server.js").write_text("test")
        (source / "node").write_text("node")
        (source / "node.sha256").write_text("digest")
        (source / "data.sqlite3").write_text("must not copy")
        home = self.root / "isolated-home"
        _copy_runtime_cache(source, home)
        target = home / ".local/share/kiro-cli"
        self.assertTrue((target / "kas" / "bundle" / "server.js").exists())
        self.assertFalse((target / "data.sqlite3").exists())
        _copy_runtime_cache(source, home)

    async def test_secret_reference_is_resolved_for_the_child_only(self):
        calls = []
        def make_session():
            def client(name, **kwargs):
                calls.append(("client", name))
                return types.SimpleNamespace(get_secret_value=lambda **values: (
                    calls.append(("secret", values["SecretId"])) or
                    {"SecretString": '{"key":"synthetic-secret-value"}'}))
            return types.SimpleNamespace(client=client)
        fake = types.SimpleNamespace(Session=make_session)
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "secret", cli_path=str(FAKE),
                                 secret_id="secret-ref", secret_json_key="key")
        with patch.dict("sys.modules", {"boto3": fake}):
            app = self.app(config)
            status, body, _ = await self.invoke(app, {"prompt": "hello"})
        self.assertEqual((status, json.loads(body)["response"]), (200, "hello"))
        session = app.state.kiro_agentcore.sessions["session-a"]
        self.assertEqual(session.client.options.env["KIRO_API_KEY"], "synthetic-secret-value")
        self.assertNotIn("synthetic-secret-value", (session.directory / "session.json").read_text())
        self.assertEqual(calls, [("client", "secretsmanager"), ("secret", "secret-ref")])


if __name__ == "__main__":
    unittest.main()
