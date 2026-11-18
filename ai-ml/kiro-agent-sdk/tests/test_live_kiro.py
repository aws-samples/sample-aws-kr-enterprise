"""Opt-in integration checks: these create Kiro sessions and consume credits."""
import os
import json
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from kiro_agent_sdk import (AgentDefinition, AgentMessage, BackgroundTaskManager, HookMatcher, InMemorySessionStore, KiroAgentOptions, KiroSDKClient, MirrorErrorMessage, PermissionResultAllow, PermissionResultDeny, ResultMessage, ProtocolError, ReportedCreditLimitError, SQLiteSessionStore, KiroEvent, TextBlock, ToolCall, UserMessage, delete_session,
                            create_mcp_server, get_session_archive_history, get_session_archive_history_from_store, get_session_archive_messages, get_session_archive_messages_from_store, get_session_history, get_session_messages, get_session_messages_from_store, get_session_info_from_store, import_session_to_store,
                            list_sessions, list_sessions_from_store, rename_session,
                            fork_session, fork_session_via_store, project_key_for_directory, tool,
                            query_with_structured_output_retry)

_LEASED_CLIENT_TOKEN = None


@unittest.skipUnless(os.getenv('KIRO_SDK_INTEGRATION') == '1' and shutil.which('kiro-cli'),
                     'set KIRO_SDK_INTEGRATION=1 with an authenticated Kiro CLI')
class KiroIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _response(self, client, prompt):
        await client.query(prompt)
        messages = [m async for m in client.receive_response()]
        result = next(m for m in messages if isinstance(m, ResultMessage))
        self.assertTrue(result.result)
        self.assertEqual(result.stop_reason, 'end_turn')
        return result

    def _optional_client_auth_test_token(self):
        import sqlite3
        from kiro_agent_sdk.kiro_client_auth import validate_client_token
        if os.getenv("KIRO_VALIDATION_CLIENT_TOKEN_FD"):
            # An optional test-harness lease: the token arrives once through a pipe.
            global _LEASED_CLIENT_TOKEN
            if _LEASED_CLIENT_TOKEN is None:
                with os.fdopen(int(os.environ["KIRO_VALIDATION_CLIENT_TOKEN_FD"]), "rb") as pipe:
                    _LEASED_CLIENT_TOKEN = validate_client_token(json.loads(pipe.read(8193)))
            return dict(_LEASED_CLIENT_TOKEN)
        db = os.getenv("KIRO_SDK_TEST_CLIENT_TOKEN_DB")
        if not db:
            self.skipTest("set KIRO_SDK_TEST_CLIENT_TOKEN_DB for client-owned auth testing")
        try:
            connection = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                row = connection.execute("select value from auth_kv where key=?", ("kirocli:odic:token",)).fetchone()
                profile_row = connection.execute("select value from state where key=?",
                                                 ("api.codewhisperer.profile",)).fetchone()
            finally:
                connection.close()
            source = json.loads(row[0])
            value = {"accessToken": source["access_token"], "expiresAt": source["expires_at"]}
            if profile_row:
                profile = json.loads(profile_row[0])
                if isinstance(profile.get("arn"), str):
                    value["profileArn"] = profile["arn"]
            return validate_client_token(value)
        except Exception:
            self.skipTest("existing Kiro client-auth test token is unavailable or near expiry")

    async def test_v3_client_owned_auth_with_existing_login(self):
        credentials = self._optional_client_auth_test_token()
        calls = []
        async def provider():
            calls.append(True)
            return dict(credentials)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            store = InMemorySessionStore()
            options = KiroAgentOptions(
                cwd=cwd, env=env, agent_engine="v3", tools=[], turn_timeout=90,
                kiro_auth_method="client", kiro_access_token_provider=provider,
                session_store=store, session_store_flush="eager")
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, "Reply CLIENT_AUTH_OK and do not use tools.")
                    if "CLIENT_AUTH_OK" not in result.result.upper() or not calls:
                        self.fail("Client-owned authentication did not complete")
                    entries = await store.load(client._store_key)
                    if credentials["accessToken"] in repr(entries):
                        self.fail("Client authentication token appeared in the session mirror")
            except Exception:
                raise AssertionError("Client-owned Kiro authentication live test failed") from None
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_session_client_waits_for_slow_token_provider(self):
        import asyncio
        from kiro_agent_sdk import KiroSessionClient
        credentials = self._optional_client_auth_test_token()
        async def provider():
            await asyncio.sleep(.5)
            return dict(credentials)
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            env = {"KIRO_HOME": str(cwd / ".kiro"), "XDG_CONFIG_HOME": str(cwd / ".config"),
                   "XDG_STATE_HOME": str(cwd / ".state"), "XDG_CACHE_HOME": str(cwd / ".cache")}
            owned = []
            try:
                async with KiroSDKClient(KiroAgentOptions(
                        cwd=cwd, env=env, agent_engine="v3", tools=[], turn_timeout=90,
                        kiro_auth_method="client", kiro_access_token_provider=provider)) as client:
                    owned.append(client.session_id)
                    await self._response(client, "Reply SESSION_CONTROL_OK and do not use tools.")
                async with KiroSessionClient(cwd=cwd, env=env, access_token_provider=provider,
                                              timeout=120) as sessions:
                    self.assertIsNotNone(await sessions.get_session_info(owned[0]))
                    await sessions.rename_session(owned[0], "Live SDK session control")
                    self.assertTrue(await sessions.get_session_messages(owned[0]))
                    forked = await sessions.fork_session(owned[0])
                    owned.append(forked.session_id)
                    for session_id in tuple(reversed(owned)):
                        await sessions.delete_session(session_id)
                        owned.remove(session_id)
            except Exception:
                raise AssertionError("Slow-provider session control live test failed") from None
            finally:
                for session_id in owned:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_client_auth_without_a_login_store(self):
        from kiro_agent_sdk.agentcore import _copy_runtime_cache
        credentials = self._optional_client_auth_test_token()
        executable = Path(shutil.which("kiro-cli")).with_name("kiro-cli-chat")
        cache = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "kiro-cli"
        if not executable.is_file() or not (cache / "node").is_file() or not (cache / "kas").is_dir():
            self.skipTest("requires the installed chat executable and public V3 runtime cache")
        calls = []
        async def provider():
            calls.append(True)
            return dict(credentials)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _copy_runtime_cache(cache, root)
            self.assertFalse((root / ".local/share/kiro-cli/data.sqlite3").exists())
            env = {"KIRO_HOME": str(root / ".kiro"), "XDG_DATA_HOME": str(root / ".local/share"),
                   "XDG_CONFIG_HOME": str(root / ".config"), "XDG_CACHE_HOME": str(root / ".cache"),
                   "XDG_STATE_HOME": str(root / ".state")}
            options = KiroAgentOptions(cwd=root, cli_path=executable, env=env, agent_engine="v3", tools=[],
                startup_timeout=120, turn_timeout=90, kiro_auth_method="client", kiro_access_token_provider=provider)
            try:
                async with KiroSDKClient(options) as client:
                    result = await self._response(client, "Reply FRESH_CLIENT_AUTH_OK and do not use tools.")
                    self.assertIn("FRESH_CLIENT_AUTH_OK", result.result.upper())
                    self.assertTrue(calls)
            except Exception:
                raise AssertionError("Client-owned auth without a login store failed") from None

    async def test_v3_authorized_idle_token_refresh_with_existing_login(self):
        import asyncio
        from datetime import datetime, timedelta, timezone
        from kiro_agent_sdk.kiro_client_auth import validate_client_token
        credentials = self._optional_client_auth_test_token()
        try:
            validate_client_token(credentials, now=datetime.now(timezone.utc) + timedelta(seconds=60))
        except ValueError:
            self.skipTest("existing login is too close to expiry for idle-refresh testing")
        calls, checks = [], []
        async def provider():
            calls.append(True)
            result = dict(credentials)
            result["expiresAt"] = (datetime.now(timezone.utc) + timedelta(seconds=190)).isoformat()
            return result
        async def authorize_idle():
            checks.append(True)
            return True
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            options = KiroAgentOptions(
                cwd=cwd, env=env, agent_engine="v3", tools=[], turn_timeout=90,
                kiro_auth_method="client", kiro_access_token_provider=provider,
                kiro_client_auth_idle_mode="authorized", kiro_idle_token_authorizer=authorize_idle)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await self._response(client, "Reply READY and do not use tools.")
                    if client._turn is not None and not client._turn.done():
                        self.fail("The test session is not idle")
                    before = len(calls)
                    await asyncio.sleep(8)
                    await client.get_kiro_account_usage()
                    async def wait():
                        while len(calls) <= before or len(checks) < 2:
                            await asyncio.sleep(.05)
                    await asyncio.wait_for(wait(), 15)
            except Exception:
                raise AssertionError("Authorized idle Kiro token refresh live test failed") from None
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "requires AgentCore")
    async def test_local_agentcore_client_owned_auth_with_existing_login(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        from test_agentcore import HEADER, request
        credentials = self._optional_client_auth_test_token()
        calls = []
        async def authorize(req, session):
            return req.headers.get("x-test-identity") == "owner" and session == "session-a"
        def token_factory(workspace, session):
            async def provider():
                calls.append(session)
                return dict(credentials)
            return provider
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "kiro-state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            def factory(workspace, resume):
                return KiroAgentOptions(tools=[], env=env, kiro_auth_method="client")
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "state", auth_mode="cli", agent_engine="v3",
                                     startup_timeout=90, turn_timeout=90)
            app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorize,
                                       kiro_token_provider_factory=token_factory)
            service, session_id, cwd = app.state.kiro_agentcore, None, None
            try:
                headers = [(HEADER, b"session-a"), (b"x-test-identity", b"owner")]
                status, body, _ = await request(app, "POST", "/invocations",
                                                b'{"prompt":"Reply CLIENT_AUTH_OK and do not use tools."}',
                                                headers, timeout=120)
                if (status != 200 or json.loads(body).get("is_error") or
                        b"CLIENT_AUTH_OK" not in body.upper() or credentials["accessToken"].encode() in body or
                        not calls or any(session != "session-a" for session in calls)):
                    self.fail("AgentCore client-owned authentication did not complete safely")
                client = service.sessions["session-a"].client
                session_id, cwd = client.session_id, Path(client.options.cwd)
            except Exception:
                raise AssertionError("AgentCore client-owned Kiro authentication live test failed") from None
            finally:
                await service.close()
                if session_id and cwd:
                    await delete_session(session_id, cwd=cwd, env=env)

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "requires AgentCore")
    async def test_local_agentcore_authorized_idle_token_refresh(self):
        import asyncio
        from datetime import datetime, timedelta, timezone
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        from kiro_agent_sdk.kiro_client_auth import validate_client_token
        from test_agentcore import HEADER, request
        credentials = self._optional_client_auth_test_token()
        try:
            validate_client_token(credentials, now=datetime.now(timezone.utc) + timedelta(seconds=60))
        except ValueError:
            self.skipTest("existing login is too close to expiry for idle-refresh testing")
        allowed, calls, checks = True, [], []
        async def authorize(req, session):
            return allowed and req.headers.get("x-test-identity") == "owner" and session == "session-a"
        async def authorize_idle(session):
            checks.append(session)
            return allowed and session == "session-a"
        def token_factory(workspace, session):
            async def provider():
                calls.append(session)
                result = dict(credentials)
                result["expiresAt"] = (datetime.now(timezone.utc) + timedelta(seconds=190)).isoformat()
                return result
            return provider
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "kiro-state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            def factory(workspace, resume):
                return KiroAgentOptions(tools=[], env=env, kiro_auth_method="client",
                                        kiro_client_auth_idle_mode="authorized")
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "state", auth_mode="cli", agent_engine="v3",
                                     startup_timeout=90, turn_timeout=90)
            app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorize,
                                       kiro_token_provider_factory=token_factory,
                                       kiro_idle_token_authorizer=authorize_idle)
            service, session_id, cwd = app.state.kiro_agentcore, None, None
            try:
                headers = [(HEADER, b"session-a"), (b"x-test-identity", b"owner")]
                status, body, _ = await request(app, "POST", "/invocations",
                                                b'{"prompt":"Reply READY and do not use tools."}', headers, timeout=120)
                if status != 200 or json.loads(body).get("is_error") or credentials["accessToken"].encode() in body:
                    self.fail("AgentCore idle authentication session failed")
                client = service.sessions["session-a"].client
                session_id, cwd = client.session_id, Path(client.options.cwd)
                before = len(calls)
                await asyncio.sleep(8)
                await client.get_kiro_account_usage()
                async def wait_for(predicate):
                    while not predicate():
                        await asyncio.sleep(.05)
                await asyncio.wait_for(wait_for(lambda: len(calls) > before and len(checks) >= 2), 15)
                allowed = False
                before_calls, before_checks = len(calls), len(checks)
                await asyncio.sleep(8)
                try:
                    await client.get_kiro_account_usage()
                except Exception:
                    pass
                await asyncio.wait_for(wait_for(lambda: len(checks) > before_checks), 15)
                if len(calls) != before_calls:
                    self.fail("Revoked AgentCore idle token was delivered")
            except Exception:
                raise AssertionError("AgentCore authorized idle token refresh live test failed") from None
            finally:
                await service.close()
                if session_id and cwd:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_prefetch_first_sends_before_response_consumption(self):
        import asyncio
        first_done = asyncio.Event()

        async def source():
            yield "Reply exactly FIRST."
            await first_done.wait()
            yield "Reply exactly SECOND."

        with tempfile.TemporaryDirectory() as cwd:
            options = KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=[], turn_timeout=90,
                                       stream_input_mode="prefetch-first")
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query(source())
                    self.assertIsNotNone(client._turn)
                    await asyncio.wait_for(asyncio.shield(client._turn), 90)
                    results = []
                    async for event in client.receive_messages():
                        if isinstance(event, ResultMessage):
                            self.assertFalse(event.is_error)
                            results.append(event.result)
                            first_done.set()
                    self.assertEqual(len(results), 2)
                    self.assertIn("FIRST", results[0].upper())
                    self.assertIn("SECOND", results[1].upper())
            finally:
                first_done.set()
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_prefetched_stream_input(self):
        import asyncio
        prepared = asyncio.Event()
        release = asyncio.Event()

        async def source():
            yield "Reply exactly FIRST."
            prepared.set()
            await release.wait()
            yield "Reply exactly SECOND."

        with tempfile.TemporaryDirectory() as cwd:
            options = KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=[], turn_timeout=90,
                                       stream_input_mode="prefetch")
            session_id = None
            try:
                client = await asyncio.to_thread(KiroSDKClient, options)
                async with client:
                    session_id = client.session_id
                    await client.query(source())
                    await asyncio.wait_for(prepared.wait(), 5)
                    results = []
                    async for event in client.receive_messages():
                        if isinstance(event, ResultMessage):
                            self.assertFalse(event.is_error)
                            results.append(event.result)
                            release.set()
                    self.assertEqual(len(results), 2)
                    self.assertIn("FIRST", results[0].upper())
                    self.assertIn("SECOND", results[1].upper())
            finally:
                release.set()
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_approved_structured_output_retry(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cwd = root / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=[], turn_timeout=90,
                system_prompt=("For the first user turn respond with exactly NOT_JSON, even if a JSON schema "
                               "is requested. For a later correction turn respond with JSON "
                               '{"answer":"RETRY_LIVE_OK"}. Do not use tools.'),
                output_format={"type": "json_schema", "schema": {
                    "type": "object", "properties": {"answer": {"const": "RETRY_LIVE_OK"}},
                    "required": ["answer"]}})
            attempts = []
            messages = []
            async def approve(error, observed_tools, attempt):
                attempts.append((error.session_id, observed_tools, attempt))
                return True
            try:
                messages = [message async for message in query_with_structured_output_retry(
                    prompt="Follow the first-turn instruction.", options=options, approve_retry=approve)]
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0][1:], (False, 1))
                result = next(message for message in messages if isinstance(message, ResultMessage))
                self.assertEqual(result.structured_output, {"answer": "RETRY_LIVE_OK"})
            finally:
                session_ids = {attempt[0] for attempt in attempts if attempt[0]}
                session_ids.update(message.session_id for message in messages
                                   if isinstance(message, ResultMessage) and message.session_id)
                for session_id in session_ids:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_code_intelligence_controls(self):
        with tempfile.TemporaryDirectory(prefix="kiro-code-live-") as root:
            root = Path(root)
            cwd = root / "workspace"
            cwd.mkdir()
            (cwd / "sample.py").write_text("def example():\n    return 42\n", encoding="utf-8")
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=[],
                                       enable_kiro_code_intelligence=True, startup_timeout=90)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    before = await client.get_kiro_code_intelligence_status()
                    self.assertTrue(before["success"])
                    self.assertFalse(before["status"]["initialized"])
                    self.assertTrue((await client.initialize_kiro_code_intelligence())["success"])
                    after = await client.get_kiro_code_intelligence_status()
                    self.assertTrue(after["status"]["initialized"])
                    self.assertTrue((cwd / ".kiro" / "settings" / "lsp.json").is_file())
                    self.assertTrue((await client.get_kiro_code_overview())["success"])
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_remote_mcp_oauth_connect(self):
        import asyncio
        from kiro_agent_sdk import McpOAuthConfig
        from test_mcp_oauth import OAuthFixture
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("optional mcp dependency not installed")
        for kind in ("http", "sse"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                fixture = OAuthFixture(kind)
                url = await fixture.start()
                cwd = Path(root) / "workspace"
                cwd.mkdir()
                env = {"KIRO_HOME": str(Path(root) / "kiro"),
                       "XDG_STATE_HOME": str(Path(root) / "state"),
                       "XDG_CONFIG_HOME": str(Path(root) / "config"),
                       "XDG_CACHE_HOME": str(Path(root) / "cache")}
                async def gate(event, tool_id, context):
                    name = event.get("tool_name", "")
                    data = event.get("tool_input") or {}
                    if ((name == "tool_load" and data.get("tool_ids") == ["oauth_probe::probe"]) or
                            ("oauth_probe" in name and "probe" in name)):
                        return {}
                    return {'block': "Only the isolated OAuth probe is allowed"}
                options = KiroAgentOptions(
                    cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"],
                    sdk_mcp_toggle_mode="sdk-all", startup_timeout=30, turn_timeout=90,
                    permission_mode="trustAll", native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                    mcp_servers={"oauth_probe": {"type": kind, "url": url}},
                    mcp_oauth={"oauth_probe": McpOAuthConfig(
                        "http://127.0.0.1:9999/callback", fixture.authorize)})
                session_id = None
                try:
                    async with KiroSDKClient(options) as client:
                        session_id = client.session_id
                        for _ in range(150):
                            status = await client.get_mcp_status()
                            if any(item["name"] == "oauth_probe" and item["status"] == "connected"
                                   for item in (status or {}).get("mcpServers", [])):
                                break
                            await asyncio.sleep(0.1)
                        else:
                            self.fail(f"{kind} OAuth MCP server did not connect")
                        self.assertEqual(fixture.token_grants, ["authorization_code"])
                        result = await self._response(client, "Call the oauth_probe probe tool once and report its "
                                                             "result. Do not use any other tools.")
                        self.assertEqual(fixture.calls, ["probe"])
                        self.assertIn("OAUTH_PROBE_OK", result.result.upper())
                        self.assertTrue((await client.reconnect_mcp_server("oauth_probe", start_oauth=True))["success"])
                        for _ in range(150):
                            if len(fixture.token_grants) == 2:
                                break
                            await asyncio.sleep(0.1)
                        else:
                            self.fail(f"{kind} OAuth restart did not request a new token")
                finally:
                    try:
                        if session_id:
                            await delete_session(session_id, cwd=cwd, env=env)
                    finally:
                        await fixture.close()

    async def test_sdk_background_task_uses_independent_kiro_session(self):
        with tempfile.TemporaryDirectory() as cwd:
            session_id = None
            task_id = None
            manager = BackgroundTaskManager()
            try:
                async with manager:
                    task_id = await manager.start("Reply with the single word READY. Do not use tools.",
                        options=KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=[], turn_timeout=90))
                    messages = [item async for item in manager.events(task_id)]
                    status = await manager.wait(task_id)
                    session_id = status.session_id
                    self.assertEqual(status.state, "completed")
                    self.assertTrue(any(isinstance(item, ResultMessage) and not item.is_error and
                                        "READY" in (item.result or "").upper() for item in messages))
            finally:
                if not session_id and task_id:
                    session_id = manager.status(task_id).session_id
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_attached_background_task_stop_with_real_kiro(self):
        import asyncio
        started, cancelled = asyncio.Event(), asyncio.Event()

        @tool("slow", "Wait for cancellation", {})
        async def slow(args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def gate(event, tool_id, context):
            name, data = event.get("tool_name", ""), event.get("tool_input") or {}
            allowed = ((name == "tool_load" and data.get("tool_ids") == ["probe_server::slow"]) or
                       name == "mcp_probe_server_slow")
            return {} if allowed else {"block": "Only the test tool is allowed"}

        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            manager = BackgroundTaskManager()
            parent_id = task_id = child_id = None
            try:
                async with manager:
                    async with KiroSDKClient(KiroAgentOptions(cwd=cwd, env=env,
                                                               agent_engine="v3", tools=[])) as client:
                        parent_id = client.session_id
                        client.attach_background_tasks(manager)
                        task_id = await manager.start("Call the probe_server slow tool once. Do not use other tools.",
                            options=KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"],
                                mcp_servers={"probe_server": create_mcp_server("probe_server", tools=[slow])},
                                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                                permission_mode="trustAll", turn_timeout=90))
                        await asyncio.wait_for(started.wait(), 60)
                        child_id = manager.status(task_id).session_id
                        await asyncio.wait_for(client.stop_task(task_id), 30)
                        self.assertEqual(manager.status(task_id).state, "cancelled")
                        await asyncio.wait_for(cancelled.wait(), 5)
            finally:
                if task_id and not child_id:
                    child_id = manager.status(task_id).session_id
                for session_id in (child_id, parent_id):
                    if session_id:
                        await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_kiro_memory_crud(self):
        with tempfile.TemporaryDirectory() as cwd:
            env = {"KIRO_HOME": str(Path(cwd) / "kiro")}
            session_id = None
            memory_id = None
            token = uuid.uuid4().hex
            scope = "repo:github.com/kiro-sdk-test/" + token
            try:
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine="v3",
                                                          tools=[], env=env)) as client:
                    session_id = client.session_id
                    if (await client.get_kiro_memory_sync_status()).get("state") != "off":
                        self.skipTest("memory sync must be off for the disposable memory probe")
                    try:
                        created = await client.create_kiro_memory(
                            memory_type="SEMANTIC", scopes=[scope], title="sdk-test-" + token,
                            summary="temporary SDK integration test", content="original")
                        memory_id = created.get("id")
                        self.assertTrue(memory_id)
                        self.assertEqual((await client.get_kiro_memory(memory_id))["memory"]["content"], "original")
                        updated = await client.update_kiro_memory(memory_id, content="updated")
                        self.assertEqual(updated["memory"]["content"], "updated")
                        listing = await client.list_kiro_memories(scopes=[scope])
                        self.assertTrue(any(item["id"] == memory_id for item in listing["memories"]))
                    finally:
                        if memory_id:
                            deleted = await client.delete_kiro_memory(memory_id)
                            self.assertEqual(deleted["id"], memory_id)
                    listing = await client.list_kiro_memories(scopes=[scope])
                    self.assertFalse(any(item["id"] == memory_id for item in listing["memories"]))
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v2_v3_set_default_model(self):
        from kiro_agent_sdk.sessions import _list_cli
        with tempfile.TemporaryDirectory() as cwd:
            listing = await _list_cli("--list-models", cwd, "kiro-cli")
            default = listing.get("default_model")
            alternate = next((model.get("model_id") for model in listing.get("models", [])
                              if isinstance(model.get("model_id"), str)
                              and model["model_id"] != default), None)
            if not isinstance(default, str) or not alternate:
                self.skipTest("CLI did not report a default and an alternative model")
            for engine in ("v2", "v3"):
                with self.subTest(engine=engine):
                    session_id = None
                    try:
                        async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine=engine,
                                                                  tools=[])) as client:
                            session_id = client.session_id
                            await client.set_model(alternate)
                            await client.set_model()
                            self.assertEqual(client._session_model, default)
                            self.assertEqual(client._session_model, default)
                            if engine == "v3":
                                self.assertTrue(any(option.get("id") == "model" and
                                                    option.get("currentValue") == default
                                                    for option in client.session_info["configOptions"]))
                    finally:
                        if session_id:
                            await delete_session(session_id, cwd=cwd)

    async def test_v3_workflow_lifecycle_without_starting_steps(self):
        with tempfile.TemporaryDirectory() as cwd:
            session_id = workflow_id = None
            try:
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine='v3',
                                                          enable_kiro_hooks=False)) as client:
                    session_id = client.session_id
                    recipe = {'name': 'sdk-probe', 'inputs': {}, 'steps': [
                        {'type': 'step', 'id': 'probe', 'agent': 'wf-planner',
                         'prompt': 'Reply OK and do not use tools.'}]}
                    try:
                        workflow_id = (await client.create_kiro_workflow(workflow=recipe))['workflowId']
                        listing = await client.list_kiro_workflows()
                        self.assertTrue(any(r['workflowId'] == workflow_id for r in listing['runs']))
                        inspected = await client.inspect_kiro_workflow(workflow_id)
                        self.assertEqual(inspected['workflowId'], workflow_id)
                        self.assertTrue((await client.cancel_kiro_workflow(workflow_id))['ok'])
                    finally:
                        if workflow_id:
                            await client.delete_kiro_workflow(workflow_id)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_cancelled_turn_is_not_reported_as_success(self):
        import asyncio
        with tempfile.TemporaryDirectory() as cwd:
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, agent_engine='v3', tools=[], turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query('Write a detailed 5000-word essay about cloud computing without tools.')
                    await asyncio.sleep(0.5)
                    await client.interrupt()
                    messages = [m async for m in client.receive_response()]
                    result = next(m for m in messages if isinstance(m, ResultMessage))
                    self.assertEqual(result.stop_reason, 'cancelled')
                    self.assertTrue(result.is_error)
                    followup = await self._response(client, 'Reply OK without tools.')
                    self.assertFalse(followup.is_error)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "agentcore extra")
    async def test_v3_agentcore_agent_initial_prompt_then_user_prompt(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        from test_agentcore import HEADER, request
        marker = "CORE_AGENT_INITIAL_" + uuid.uuid4().hex[:10].upper()
        async def authorizer(req, session_id):
            return True
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "agentcore", cli_path=shutil.which("kiro-cli"),
                auth_mode="cli", agent_engine="v3", startup_timeout=90, turn_timeout=90)
            def factory(workspace, resume):
                return KiroAgentOptions(agent="initial_agent", env=env, agents={
                    "initial_agent": AgentDefinition("Test", "Follow the user request.", tools=[],
                        initial_prompt="Remember " + marker + ". Reply INIT_OK without tools.")})
            app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
            session_id, workspace = None, None
            try:
                payload = json.dumps({"prompt": "What marker were you asked to remember? Do not use tools."}).encode()
                status, body, _ = await request(app, "POST", "/invocations", payload,
                    headers=[(HEADER, b"session-a"), (b"content-type", b"application/json")], timeout=180)
                self.assertEqual(status, 200)
                result = json.loads(body)
                self.assertFalse(result["is_error"])
                self.assertIn(marker, result["response"].upper())
                self.assertNotIn("INIT_OK", result["response"].upper())
                session_id = result["kiro_session_id"]
                workspace = app.state.kiro_agentcore.sessions["session-a"].directory / "workspace"
            finally:
                await app.state.kiro_agentcore.close()
                if session_id and workspace:
                    await delete_session(session_id, cwd=workspace, env=env)

    async def test_v3_multiple_native_stop_failure_policies(self):
        calls = []

        async def first(event, tool_id, context):
            calls.append("first")
            raise RuntimeError("synthetic hook failure")

        async def second(event, tool_id, context):
            calls.append("second")
            return {}

        with tempfile.TemporaryDirectory() as cwd:
            options = KiroAgentOptions(
                cwd=cwd, agent_engine="v3", tools=[], turn_timeout=90,
                native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                hooks={"agentStop": [HookMatcher(hooks=[first, second], timeout=1, on_failure="continue")]})
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await self._response(client, "Reply OK without using tools.")
                    self.assertEqual(calls, ["first", "second"])
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_interrupt_cancels_pending_permission(self):
        import asyncio

        started, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            target = cwd / "probe.txt"
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}

            async def gate(event, tool_id, context):
                name = event.get("tool_name", "").lower()
                data = json.dumps(event.get("tool_input") or {}).lower()
                allowed = ((name == "tool_load" and "write" in data) or
                           ("probe.txt" in data and "write" in name))
                return {} if allowed else {"block": "Only the test file is allowed"}

            async def approve(name, input_data, context):
                ids = input_data.get("tool_ids")
                if isinstance(ids, list) and ids and all("write" in str(item).lower() for item in ids):
                    return PermissionResultAllow()
                consent = context.kiro_consent or {}
                if consent.get("capability") != "fs_write":
                    return PermissionResultDeny()
                started.set()
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    await released.wait()
                finished.set()
                return PermissionResultAllow()

            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["write"],
                    kiro_permissions={"rules": [{"capability": "fs_write", "effect": "ask"}]},
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, can_use_tool=approve, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query("Write TEST to probe.txt. Do not use other files or tools.")
                    await asyncio.wait_for(started.wait(), 60)
                    await client.interrupt()
                    result = [message async for message in client.receive_response()][-1]
                    self.assertTrue(result.is_error)
                    released.set()
                    await asyncio.wait_for(finished.wait(), 5)
                    self.assertFalse(target.exists())
                    followup = await self._response(client, "Reply OK without using tools.")
                    self.assertFalse(followup.is_error)
            finally:
                released.set()
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_interrupt_cancels_sdk_tools_when_enabled(self):
        import asyncio

        started, cancelled, released, finished = (asyncio.Event() for _ in range(4))

        @tool("slow", "Wait for cancellation", {})
        async def slow(args):
            started.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await released.wait()
            finished.set()
            return {"content": [{"type": "text", "text": "LATE_TOOL_RESULT"}]}

        async def gate(event, tool_id, context):
            name, data = event.get("tool_name", ""), event.get("tool_input") or {}
            allowed = ((name == "tool_load" and data.get("tool_ids") == ["probe_server::slow"]) or
                       name == "mcp_probe_server_slow")
            return {} if allowed else {"block": "Only the test tool is allowed"}

        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"],
                    mcp_servers={"probe_server": create_mcp_server("probe_server", tools=[slow])},
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                    permission_mode="trustAll", sdk_tool_interrupt_mode="cancel-all", turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query("Call the probe_server slow tool once. Do not use other tools.")
                    await asyncio.wait_for(started.wait(), 60)
                    await client.interrupt()
                    result = [message async for message in client.receive_response()][-1]
                    self.assertTrue(result.is_error)
                    await asyncio.wait_for(cancelled.wait(), 5)
                    released.set()
                    await asyncio.wait_for(finished.wait(), 5)
                    self.assertNotIn("LATE_TOOL_RESULT", result.result or "")
            finally:
                released.set()
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v2_interrupt_cancels_sdk_tools_when_enabled(self):
        import asyncio

        started, cancelled, released = asyncio.Event(), asyncio.Event(), asyncio.Event()

        @tool("slow", "Wait for cancellation", {})
        async def slow(args):
            started.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await released.wait()
            return {"content": [{"type": "text", "text": "LATE_TOOL_RESULT"}]}

        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v2", tools=["@probe_server"],
                    mcp_servers={"probe_server": create_mcp_server("probe_server", tools=[slow])},
                    permission_mode="trustAll", sdk_tool_interrupt_mode="cancel-all", turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query("Call the probe_server slow tool once. Do not use other tools.")
                    await asyncio.wait_for(started.wait(), 60)
                    await client.interrupt()
                    result = [message async for message in client.receive_response()][-1]
                    self.assertTrue(result.is_error)
                    await asyncio.wait_for(cancelled.wait(), 5)
            finally:
                released.set()
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_selected_model_is_reported_on_assistant_messages(self):
        with tempfile.TemporaryDirectory() as cwd:
            session_id = None
            try:
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine='v3', tools=[])) as client:
                    session_id = client.session_id
                    options = client.session_info.get('configOptions', [])
                    model = next((o.get('currentValue') for o in options if o.get('id') == 'model'), None)
                    if not isinstance(model, str) or not model:
                        self.skipTest('Kiro did not report a selected model')
                    await client.set_config_option('model', model)
                    await client.query('Reply OK without tools.')
                    messages = [m async for m in client.receive_response()]
                    assistant = next(m for m in messages if isinstance(m, AgentMessage))
                    self.assertEqual(assistant.model, model)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_sdk_prompt_hook_additional_context(self):
        async def add_context(event, tool_id, context):
            return {'context': 'The requested marker is LILAC621.'}
        with tempfile.TemporaryDirectory() as cwd:
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, agent_engine='v3', tools=[],
                    hooks={'promptSubmit': [HookMatcher(hooks=[add_context])]})
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, 'Report the requested marker only. Do not use tools.')
                    self.assertIn('LILAC621', result.result.upper())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_tool_metadata_is_available_on_content_block(self):
        with tempfile.TemporaryDirectory() as cwd:
            (Path(cwd) / 'probe.txt').write_text('PURPLE971')
            session_id = None
            async def gate(event, tool_id, context):
                name = event.get('tool_name', '')
                data = json.dumps(event.get('tool_input') or {})
                allowed = ((name == 'tool_load' and 'read' in data) or
                           ('probe.txt' in data and 'read' in name.lower()))
                return {} if allowed else {"block": 'Only the isolated test file is allowed'}
            try:
                options = KiroAgentOptions(cwd=cwd, agent_engine='v3', tools=['read'],
                                           native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                                           permission_mode='trustAll', turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query('Read probe.txt and report its contents. Do not use other files or tools.')
                    messages = [m async for m in client.receive_response()]
                    blocks = [message for message in messages if isinstance(message, ToolCall)]
                    events = [m.data for m in messages if isinstance(m, KiroEvent) and m.kind == 'tool_call']
                    matches = [(block, event) for block in blocks for event in events
                               if block.tool_call_id == event.get('toolCallId') and
                               isinstance(event.get('_meta', {}).get('kiro'), dict)]
                    self.assertTrue(matches)
                    self.assertEqual(matches[0][0].kiro_metadata, matches[0][1]['_meta']['kiro'])
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_agent_tool_and_deny_mappings(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            target = cwd / "probe.txt"
            target.write_text("MAPPED_AGENT_MARKER")
            denied_target = cwd / "denied.txt"
            denied_target.write_text("SYNTHETIC_DENIED_MARKER")
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            deny = [{"capability": name, "effect": "deny"}
                    for name in ("shell", "fs_write", "web_fetch", "web_search", "mcp", "subagent")]
            definition = AgentDefinition(
                "Read an isolated synthetic file", "Read only the requested test file.",
                tools=["Read"], tools_mode="selectors", tool_selector_map={"Read": ["read"]},
                disallowed_tools=["Read(denied.txt)"], disallowed_tools_mode="v3-permissions",
                disallowed_tool_permission_map={"Read(denied.txt)": [
                    {"capability": "fs_read", "effect": "deny", "match": [str(denied_target)]}]},
                kiro_permissions={"rules": [
                    *deny, {"capability": "fs_read", "effect": "allow", "match": [str(target)]}]})
            options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", agent="worker",
                agents={"worker": definition}, enable_kiro_hooks=False, turn_timeout=90)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    profile = json.loads((Path(root) / "kiro" / "agents" / "worker.json").read_text())
                    self.assertEqual(profile["tools"], ["read"])
                    self.assertEqual((await client.explain_kiro_permission("fs_read", str(target)))["effect"],
                                     "allow")
                    self.assertEqual((await client.explain_kiro_permission("fs_read", str(denied_target)))["effect"],
                                     "deny")
                    with self.assertRaises(ProtocolError):
                        await client.set_mode("vibe")
                    result = await self._response(client, "Read probe.txt and report its contents. "
                                                         "Do not use other files or tools.")
                    self.assertIn("MAPPED_AGENT_MARKER", result.result)
                from dataclasses import replace
                async with KiroSDKClient(replace(options, resume=session_id)) as resumed:
                    self.assertEqual((await resumed.explain_kiro_permission("fs_read", str(denied_target)))["effect"],
                                     "deny")
                    with self.assertRaises(ProtocolError):
                        await resumed.set_mode("vibe")
                    result = await self._response(resumed, "Reply with MAPPED_RESUME_OK. Do not use tools.")
                    self.assertIn("MAPPED_RESUME_OK", result.result.upper())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_delegated_agent_tool_mapping(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            target = cwd / "probe.txt"
            target.write_text("MAPPED_DELEGATED_MARKER")
            denied_target = cwd / "denied.txt"
            denied_target.write_text("SYNTHETIC_DENIED_MARKER")
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            seen = []
            async def gate(event, tool_id, context):
                name, data = event.get("tool_name", ""), json.dumps(event.get("tool_input") or {})
                seen.append((name, data))
                allowed = ((name == "tool_load" and ("subagent" in data or "read" in data)) or
                           name in ("subagent_worker", "subagent_response") or
                           (("probe.txt" in data or "denied.txt" in data) and "read" in name.lower()))
                return {} if allowed else {"block": "Only the test subagent is allowed"}
            deny = [{"capability": name, "effect": "deny"}
                    for name in ("shell", "fs_write", "web_fetch", "web_search", "mcp")]
            allow_read = {"capability": "fs_read", "effect": "allow"}
            definition = AgentDefinition(
                "Read isolated synthetic files", "Read probe.txt, then attempt denied.txt, and report the results.",
                tools=["Read"], tools_mode="selectors", tool_selector_map={"Read": ["read"]},
                disallowed_tools=["Read(denied.txt)"], disallowed_tools_mode="v3-permissions",
                disallowed_tool_permission_map={"Read(denied.txt)": [
                    {"capability": "fs_read", "effect": "deny", "match": [str(denied_target)]}]},
                kiro_permissions={"rules": [*deny, allow_read]})
            options = KiroAgentOptions(
                cwd=cwd, env=env, agent_engine="v3", tools=["subagent"], agents={"worker": definition},
                kiro_permissions={"rules": [*deny, allow_read, {"capability": "subagent", "effect": "allow"}]},
                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, turn_timeout=120)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, "Use the worker subagent to read probe.txt, then attempt "
                        "denied.txt, and report both results. Do not use other tools or read files yourself.")
                    self.assertIn("MAPPED_DELEGATED_MARKER", result.result)
                    self.assertNotIn("SYNTHETIC_DENIED_MARKER", result.result)
                    self.assertTrue("denied" in result.result.lower() or any(
                        "denied.txt" in data and "read" in name.lower() for name, data in seen))
                    self.assertTrue(any(name == "subagent_worker" for name, _ in seen))
                    profile = json.loads((Path(root) / "kiro" / "agents" / "worker.json").read_text())
                    self.assertEqual(profile["tools"], ["read"])
                    self.assertTrue(any(rule.get("effect") == "deny" and
                                        rule.get("match") == [str(denied_target)]
                                        for rule in profile["permissions"]["rules"]))
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_pretool_hook_permission_decision_deny(self):
        with tempfile.TemporaryDirectory() as cwd:
            target = Path(cwd) / 'probe.txt'
            session_id = None
            seen = []
            seen_ids = []
            async def gate(event, tool_id, context):
                name = event.get('tool_name', '').lower()
                data = json.dumps(event.get('tool_input') or {}).lower()
                allowed = ((name == 'tool_load' and 'write' in data) or
                           ('probe.txt' in data and 'write' in name))
                return {} if allowed else {"block": 'Only the isolated test file is allowed'}
            async def deny(event, tool_id, context):
                seen.append(event)
                seen_ids.append((tool_id, event.get('tool_call_id')))
                return {'block': 'REJECTIONINDIGO417'}
            try:
                options = KiroAgentOptions(cwd=cwd, agent_engine='v3', tools=['write'],
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, permission_mode='trustAll',
                    hooks={'preToolUse': [HookMatcher(hooks=[deny])]}, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, 'Write exactly TEST to probe.txt. Do not use other files or tools. '
                                                          'If permission is denied, quote the exact rejection reason.')
                    self.assertTrue(seen)
                    self.assertTrue(any(isinstance(ident, str) and ident and ident == payload_id
                                        for ident, payload_id in seen_ids))
                    self.assertFalse(target.exists())
                    self.assertTrue(result.permission_denials)
                    self.assertTrue(any(denial['tool_input'] == (event.get('tool_input') or {})
                                        for denial in result.permission_denials for event in seen))
                    self.assertIn('REJECTIONINDIGO417', result.result.upper())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_native_sdk_pretool_denies_trusted_write(self):
        with tempfile.TemporaryDirectory() as cwd:
            target = Path(cwd) / "probe.txt"
            session_id = None
            seen = []

            async def gate(event, tool_id, context):
                name = event.get("tool_name", "").lower()
                data = json.dumps(event.get("tool_input") or {}).lower()
                seen.append((event.get("hook_event_name"), name))
                if name == "tool_load" and "write" in data:
                    return {}
                return {'block': "BLOCKED_BY_SDK"}

            try:
                options = KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=["write"],
                    permission_mode="trustAll", native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native",
                    hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await self._response(client, "Write exactly TEST to probe.txt. Do not use other files or tools. "
                                                 "If denied, report the denial.")
                    self.assertTrue(seen)
                    self.assertTrue(all(event == "PreToolUse" for event, _ in seen))
                    self.assertTrue(any("write" in name for _, name in seen))
                    self.assertFalse(target.exists())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP dependency")
    async def test_v3_external_mcp_input_transform_reaches_servers(self):
        from test_remote_mcp_toggle import TestMcpHttpServer
        for kind in ("stdio", "http", "sse"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cwd = root / "workspace"
                cwd.mkdir()
                marker = "PRETOOL_EXTERNAL_" + uuid.uuid4().hex[:10].upper()
                service = None
                if kind == "stdio":
                    script = root / "server.py"
                    script.write_text(
                        "import json,sys\nfrom pathlib import Path\n"
                        "for line in sys.stdin:\n"
                        " r=json.loads(line); m=r.get('method'); i=r.get('id')\n"
                        " if i is None: continue\n"
                        " if m=='initialize': x={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},"
                        "'serverInfo':{'name':'probe_server','version':'1'}}\n"
                        " elif m=='tools/list': x={'tools':[{'name':'probe','description':'Return a result',"
                        "'inputSchema':{'type':'object'}}]}\n"
                        " elif m=='tools/call':\n"
                        "  Path(sys.argv[1]).write_text(json.dumps(r['params']['arguments']))\n"
                        "  x={'content':[{'type':'text','text':'remote-result'}]}\n"
                        " else: x={}\n"
                        " print(json.dumps({'jsonrpc':'2.0','id':i,'result':x}),flush=True)\n")
                    config = {"command": sys.executable, "args": [str(script), str(root / "arguments")]}
                    mode = "sdk-and-stdio"
                else:
                    service = TestMcpHttpServer(kind, protocol_version="2025-03-26")
                    url = await service.start()
                    config = {"type": kind, "url": url, "headers": {"Authorization": "Bearer fixture-secret"}}
                    mode = "sdk-all"
                observed = []
                async def transform(server_name, name, arguments):
                    observed.append((server_name, name))
                    return {"value": marker}
                async def gate(event, tool_id, context):
                    name, data = event.get("tool_name", ""), event.get("tool_input") or {}
                    allowed = ((name == "tool_load" and data.get("tool_ids") == ["probe_server::probe"]) or
                               name == "mcp_probe_server_probe")
                    return {} if allowed else {"block": "Only the test tool is allowed"}
                async def decide(name, input_data, context):
                    offered = context.kiro_consent or {}
                    allowed = ((name == "tool_load" and input_data.get("tool_ids") == ["probe_server::probe"]) or
                               (offered.get("capability") == "mcp" and "probe_server" in
                                str(offered.get("triggeringResource") or offered.get("resource"))))
                    return PermissionResultAllow() if allowed else PermissionResultDeny()
                env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                       "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"], turn_timeout=90,
                    mcp_servers={"probe_server": config}, sdk_mcp_toggle_mode=mode,
                    sdk_mcp_input_transforms={"probe_server": transform},
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, can_use_tool=decide)
                session_id = None
                try:
                    async with KiroSDKClient(options) as client:
                        session_id = client.session_id
                        result = await self._response(client, "Call probe_server::probe and return its exact result. "
                                                              "Do not use other tools.")
                    self.assertIn("remote-result", result.result.lower())
                    self.assertEqual(observed, [("probe_server", "probe")])
                    arguments = (json.loads((root / "arguments").read_text()) if service is None else
                                 next(item["params"]["arguments"] for item in service.requests
                                      if item.get("method") == "tools/call"))
                    self.assertEqual(arguments, {"value": marker})
                finally:
                    if session_id:
                        await delete_session(session_id, cwd=cwd, env=env)
                    if service is not None:
                        await service.close()

    async def test_v3_sdk_mcp_output_transform_reaches_model(self):
        marker = "TRANSFORMED_" + uuid.uuid4().hex[:10].upper()
        calls = []
        @tool("probe", "Return a verification marker", {})
        async def probe(args):
            return {"content": [{"type": "text", "text": "ORIGINAL_PRIVATE_MARKER"}]}
        async def transform(server, name, arguments, result):
            calls.append((server, name, result["content"][0]["text"]))
            return {"content": [{"type": "text", "text": marker}]}
        async def gate(event, tool_id, context):
            name = event.get("tool_name", "")
            data = event.get("tool_input") or {}
            allowed = ((name == "tool_load" and data.get("tool_ids") == ["probe_server::probe"]) or
                       name == "mcp_probe_server_probe")
            return {} if allowed else {"block": "Only the test tool is allowed"}
        async def decide(name, input_data, context):
            offered = context.kiro_consent or {}
            allowed = ((name == "tool_load" and input_data.get("tool_ids") == ["probe_server::probe"]) or
                       (offered.get("capability") == "mcp" and "probe_server" in
                        str(offered.get("triggeringResource") or offered.get("resource"))))
            return PermissionResultAllow() if allowed else PermissionResultDeny()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cwd = root / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"], turn_timeout=90,
                mcp_servers={"probe_server": create_mcp_server("probe_server", tools=[probe])},
                sdk_mcp_output_transforms={"probe_server": transform},
                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, can_use_tool=decide)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, "Call probe_server::probe, then return exactly its result. "
                                                          "Do not use other tools.")
                self.assertEqual(calls, [("probe_server", "probe", "ORIGINAL_PRIVATE_MARKER")])
                self.assertIn(marker, result.result)
                self.assertNotIn("ORIGINAL_PRIVATE_MARKER", result.result)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP dependency")
    async def test_v3_official_mcp_output_transform_reaches_model(self):
        import mcp.types as types
        from mcp.server import Server
        marker = "INSTANCE_TRANSFORMED_" + uuid.uuid4().hex[:10].upper()
        info = types.Tool(name="probe", description="Return a verification marker",
                          inputSchema={"type": "object", "properties": {}})
        async def result():
            return types.CallToolResult(content=[types.TextContent(type="text", text="ORIGINAL_INSTANCE")])
        if hasattr(types, "jsonrpc_message_adapter"):
            async def list_tools(ctx, params):
                return types.ListToolsResult(tools=[info])
            async def call_tool(ctx, params):
                return await result()
            server = Server("probe_server", on_list_tools=list_tools, on_call_tool=call_tool)
        else:
            server = Server("probe_server")
            @server.list_tools()
            async def list_tools():
                return [info]
            @server.call_tool()
            async def call_tool(name, arguments):
                return await result()
        calls = []
        async def transform(server_name, name, arguments, output):
            calls.append((server_name, name, output["content"][0]["text"]))
            return {"content": [{"type": "text", "text": marker}]}
        async def gate(event, tool_id, context):
            name = event.get("tool_name", "")
            data = event.get("tool_input") or {}
            allowed = ((name == "tool_load" and data.get("tool_ids") == ["probe_server::probe"]) or
                       name == "mcp_probe_server_probe")
            return {} if allowed else {"block": "Only the test tool is allowed"}
        async def decide(name, input_data, context):
            offered = context.kiro_consent or {}
            allowed = ((name == "tool_load" and input_data.get("tool_ids") == ["probe_server::probe"]) or
                       (offered.get("capability") == "mcp" and "probe_server" in
                        str(offered.get("triggeringResource") or offered.get("resource"))))
            return PermissionResultAllow() if allowed else PermissionResultDeny()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cwd = root / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"], turn_timeout=90,
                mcp_servers={"probe_server": {"type": "sdk", "name": "probe_server", "instance": server}},
                sdk_mcp_output_transforms={"probe_server": transform},
                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, can_use_tool=decide)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    response = await self._response(client, "Call probe_server::probe, then return exactly its result. "
                                                            "Do not use other tools.")
                self.assertEqual(calls, [("probe_server", "probe", "ORIGINAL_INSTANCE")])
                self.assertIn(marker, response.result)
                self.assertNotIn("ORIGINAL_INSTANCE", response.result)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_external_stdio_mcp_output_transform_reaches_model(self):
        marker = "STDIO_TRANSFORMED_" + uuid.uuid4().hex[:10].upper()
        calls = []
        async def transform(server_name, name, arguments, output):
            calls.append((server_name, name, output["content"][0]["text"]))
            return {"content": [{"type": "text", "text": marker}]}
        async def gate(event, tool_id, context):
            name = event.get("tool_name", "")
            data = event.get("tool_input") or {}
            allowed = ((name == "tool_load" and data.get("tool_ids") == ["probe_server::probe"]) or
                       name == "mcp_probe_server_probe")
            return {} if allowed else {"block": "Only the test tool is allowed"}
        async def decide(name, input_data, context):
            offered = context.kiro_consent or {}
            allowed = ((name == "tool_load" and input_data.get("tool_ids") == ["probe_server::probe"]) or
                       (offered.get("capability") == "mcp" and "probe_server" in
                        str(offered.get("triggeringResource") or offered.get("resource"))))
            return PermissionResultAllow() if allowed else PermissionResultDeny()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cwd = root / "workspace"
            cwd.mkdir()
            script = root / "server.py"
            script.write_text(
                "import json,sys\n"
                "for line in sys.stdin:\n"
                " r=json.loads(line); method=r.get('method'); ident=r.get('id')\n"
                " if ident is None: continue\n"
                " if method=='initialize':\n"
                "  result={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},"
                "'serverInfo':{'name':'probe_server','version':'1'}}\n"
                " elif method=='tools/list':\n"
                "  result={'tools':[{'name':'probe','description':'Return a marker',"
                "'inputSchema':{'type':'object','properties':{}}}]}\n"
                " elif method=='tools/call': result={'content':[{'type':'text','text':'ORIGINAL_STDIO'}]}\n"
                " elif method=='ping': result={}\n"
                " else:\n"
                "  print(json.dumps({'jsonrpc':'2.0','id':ident,'error':{'code':-32601,"
                "'message':'unsupported'}}),flush=True); continue\n"
                " print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result}),flush=True)\n",
                encoding="utf-8")
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"], turn_timeout=90,
                mcp_servers={"probe_server": {"command": sys.executable, "args": [str(script)]}},
                sdk_mcp_toggle_mode="sdk-and-stdio", sdk_mcp_output_transforms={"probe_server": transform},
                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, can_use_tool=decide)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    response = await self._response(client, "Call probe_server::probe, then return exactly its result. "
                                                            "Do not use other tools.")
                self.assertEqual(calls, [("probe_server", "probe", "ORIGINAL_STDIO")])
                self.assertIn(marker, response.result)
                self.assertNotIn("ORIGINAL_STDIO", response.result)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP dependency")
    async def test_v3_remote_mcp_output_transform_reaches_model(self):
        from test_remote_mcp_toggle import TestMcpHttpServer
        for kind in ("http", "sse"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                cwd = root / "workspace"
                cwd.mkdir()
                service = TestMcpHttpServer(kind, protocol_version="2025-03-26")
                url = await service.start()
                marker = "REMOTE_TRANSFORMED_" + uuid.uuid4().hex[:10].upper()
                calls, gated = [], []
                async def transform(server_name, name, arguments, output):
                    calls.append((server_name, name, output["content"][0]["text"]))
                    return {"content": [{"type": "text", "text": marker}]}
                def probe_load(data):
                    ids = data.get("tool_ids") if isinstance(data, dict) else None
                    # Loading only this server's tool is allowed, however Kiro spells the ID list.
                    return (isinstance(ids, list) and ids and
                            all(isinstance(item, str) and item.startswith("probe_server") for item in ids))
                async def gate(event, tool_id, context):
                    name = event.get("tool_name", "")
                    data = event.get("tool_input") or {}
                    allowed = (name == "tool_load" and probe_load(data)) or name == "mcp_probe_server_probe"
                    gated.append((name, allowed))
                    return {} if allowed else {"block": "Only the test tool is allowed"}
                async def decide(name, input_data, context):
                    offered = context.kiro_consent or {}
                    allowed = ((name == "tool_load" and probe_load(input_data)) or
                               (offered.get("capability") == "mcp" and "probe_server" in
                                str(offered.get("triggeringResource") or offered.get("resource"))))
                    return PermissionResultAllow() if allowed else PermissionResultDeny()
                env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                       "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"], turn_timeout=90,
                    mcp_servers={"probe_server": {"type": kind, "url": url,
                        "headers": {"Authorization": "Bearer fixture-secret"}}},
                    sdk_mcp_toggle_mode="sdk-all", sdk_mcp_output_transforms={"probe_server": transform},
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, can_use_tool=decide)
                session_id = None
                try:
                    async with KiroSDKClient(options) as client:
                        session_id = client.session_id
                        response = await self._response(client, "Call probe_server::probe, then return exactly its "
                                                                "result. Do not use other tools.")
                    if not calls:
                        self.skipTest(f"{kind}: the model did not call the remote MCP tool; gate saw {gated[:8]}")
                    # The model may call the tool more than once; every call must be transformed.
                    self.assertTrue(all(call == ("probe_server", "probe", "remote-result") for call in calls), calls)
                    self.assertIn(marker, response.result)
                    self.assertNotIn("remote-result", response.result)
                    self.assertFalse(service.bad_auth)
                finally:
                    if session_id:
                        await delete_session(session_id, cwd=cwd, env=env)
                    await service.close()

    async def test_v3_explicit_session_consent_for_sdk_tool(self):
        import uuid

        marker = 'CONSENT' + uuid.uuid4().hex[:10].upper()
        decisions = []

        @tool('probe', 'Return a verification marker', {})
        async def probe(args):
            return {'content': [{'type': 'text', 'text': marker}]}

        async def gate(event, tool_id, context):
            name = event.get('tool_name', '')
            data = event.get('tool_input') or {}
            allowed = ((name == 'tool_load' and data.get('tool_ids') == ['probe_server::probe']) or
                       name == 'mcp_probe_server_probe')
            return {} if allowed else {"block": 'Only the test tool is allowed'}

        async def decide(name, input_data, context):
            offered = context.kiro_consent
            choices = context.options
            decisions.append((name, offered, choices))
            if name == 'tool_load' and input_data.get('tool_ids') == ['probe_server::probe']:
                return PermissionResultAllow()
            selected = next((option for option in choices if option.get('kind') == 'allow_always'), None)
            resource = (offered or {}).get('triggeringResource') or (offered or {}).get('resource')
            if (selected and (offered or {}).get('capability') == 'mcp' and
                    isinstance(resource, str) and 'probe_server' in resource):
                return PermissionResultAllow(option_id=selected['optionId'],
                                             kiro_consent={'scope': 'session', 'resource': resource})
            return PermissionResultDeny()

        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / 'workspace'
            cwd.mkdir()
            env = {'KIRO_HOME': str(Path(root) / 'kiro'),
                   'XDG_STATE_HOME': str(Path(root) / 'state'),
                   'XDG_CONFIG_HOME': str(Path(root) / 'config'),
                   'XDG_CACHE_HOME': str(Path(root) / 'cache')}
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine='v3', tools=['@mcp'],
                    mcp_servers={'probe_server': create_mcp_server('probe_server', tools=[probe])},
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, can_use_tool=decide, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, 'Call the probe_server probe tool and return its exact result. '
                                                          'Do not use other tools.')
                    self.assertIn(marker, result.result, f'Permission requests: {decisions!r}')
                    rules = (await client.list_kiro_permissions()).get('rules', [])
                    self.assertTrue(any(rule.get('scope') == 'session' and rule.get('capability') == 'mcp'
                                        and rule.get('effect') == 'allow' for rule in rules),
                                    f'No session rule reported; permission requests: {decisions!r}')
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_shell_command_edit_is_rechecked(self):
        import uuid

        before = 'BEFORE' + uuid.uuid4().hex[:8].upper()
        after = 'AFTER' + uuid.uuid4().hex[:8].upper()
        original, replacement = f'printf {before}', f'printf {after}'
        requests = []

        async def gate(event, tool_id, context):
            name, data = event.get('tool_name', ''), event.get('tool_input') or {}
            ids = data.get('tool_ids')
            if name == 'tool_load' and isinstance(ids, list) and ids and all(
                item in ('shell', 'execute_bash') for item in ids
            ):
                return {}
            if ('shell' in name or 'bash' in name) and data.get('command') in (original, replacement):
                return {}
            return {'block': 'Only the isolated printf commands are allowed'}

        async def decide(name, input_data, context):
            requests.append((name, input_data, context.kiro_consent))
            consent = context.kiro_consent or {}
            command = input_data.get('command') or consent.get('triggeringResource') or consent.get('resource')
            if command == original:
                return PermissionResultAllow(updated_input={**input_data, 'command': replacement})
            if command == replacement:
                return PermissionResultAllow()
            ids = input_data.get('tool_ids')
            if isinstance(ids, list) and ids and all(item in ('shell', 'execute_bash') for item in ids):
                return PermissionResultAllow()
            return PermissionResultDeny()

        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / 'workspace'
            cwd.mkdir()
            env = {'KIRO_HOME': str(Path(root) / 'kiro'),
                   'XDG_STATE_HOME': str(Path(root) / 'state'),
                   'XDG_CONFIG_HOME': str(Path(root) / 'config'),
                   'XDG_CACHE_HOME': str(Path(root) / 'cache')}
            session_id = None
            try:
                options = KiroAgentOptions(
                    cwd=cwd, env=env, agent_engine='v3', tools=['shell'], kiro_shell_edit=True,
                    kiro_permissions={'rules': [{'capability': 'shell', 'effect': 'ask'}]},
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, can_use_tool=decide, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, f'Run exactly this shell command: {original}. '
                                                          'Do not run any other command. Report its output.')
                    self.assertIn(after, result.result.upper(), f'Permission requests: {requests!r}')
                    commands = [entry[1].get('command') or (entry[2] or {}).get('triggeringResource')
                                or (entry[2] or {}).get('resource') for entry in requests]
                    self.assertIn(original, commands)
                    self.assertIn(replacement, commands)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_shell_command_edit_from_hook(self):
        import uuid

        before = 'BEFORE' + uuid.uuid4().hex[:8].upper()
        after = 'AFTER' + uuid.uuid4().hex[:8].upper()
        original, replacement = f'printf {before}', f'printf {after}'
        observed = []

        async def gate(event, tool_id, context):
            name, data = event.get('tool_name', ''), event.get('tool_input') or {}
            ids = data.get('tool_ids')
            if name == 'tool_load' and isinstance(ids, list) and ids and all(
                item in ('shell', 'execute_bash') for item in ids
            ):
                return {}
            if ('shell' in name or 'bash' in name) and data.get('command') in (original, replacement):
                return {}
            return {'block': 'Only the isolated printf commands are allowed'}

        async def edit(event, tool_id, context):
            observed.append(event)
            name, data = event.get('tool_name', ''), event.get('tool_input') or {}
            if name == original:
                return {'allow': True, 'tool_input': {**data, 'command': replacement}}
            if name == replacement or (name == 'tool_load' and data.get('tool_ids') in (['shell'], ['execute_bash'])):
                return {'allow': True}
            return {'block': True}

        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / 'workspace'
            cwd.mkdir()
            env = {'KIRO_HOME': str(Path(root) / 'kiro'),
                   'XDG_STATE_HOME': str(Path(root) / 'state'),
                   'XDG_CONFIG_HOME': str(Path(root) / 'config'),
                   'XDG_CACHE_HOME': str(Path(root) / 'cache')}
            session_id = None
            try:
                options = KiroAgentOptions(
                    cwd=cwd, env=env, agent_engine='v3', tools=['shell'], kiro_shell_edit=True,
                    kiro_permissions={'rules': [{'capability': 'shell', 'effect': 'ask'}]},
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                    hooks={'preToolUse': [HookMatcher(hooks=[edit])]}, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, f'Run exactly this shell command: {original}. '
                                                          'Do not run any other command. Report its output.')
                    self.assertIn(after, result.result.upper(), f'Observed requests: {observed!r}')
                    self.assertIn(original, [event.get('tool_name') for event in observed])
                    self.assertIn(replacement, [event.get('tool_name') for event in observed])
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_nonfatal_session_mirror_failure(self):
        class FlakyStore(InMemorySessionStore):
            failed = False
            async def append(self, key, entries):
                if not self.failed:
                    self.failed = True
                    raise RuntimeError('synthetic mirror failure')
                await super().append(key, entries)
        with tempfile.TemporaryDirectory() as cwd:
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, agent_engine='v3', tools=[], session_store=FlakyStore(),
                                           session_store_error_policy='continue')
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query('Reply OK without tools.')
                    messages = [m async for m in client.receive_response()]
                    self.assertTrue(any(isinstance(m, MirrorErrorMessage) for m in messages))
                    self.assertFalse(next(m for m in messages if isinstance(m, ResultMessage)).is_error)
                    self.assertEqual(client.last_mirror_error.error, 'synthetic mirror failure')
                    followup = await self._response(client, 'Reply OK without tools.')
                    self.assertFalse(followup.is_error)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_session_helpers_use_custom_environment(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / 'workspace'
            cwd.mkdir()
            env = {'KIRO_HOME': str(Path(root) / 'kiro'),
                   'XDG_STATE_HOME': str(Path(root) / 'state'),
                   'XDG_CONFIG_HOME': str(Path(root) / 'config'),
                   'XDG_CACHE_HOME': str(Path(root) / 'cache')}
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine='v3', enable_kiro_hooks=False)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                self.assertIn(session_id, {s.session_id for s in await list_sessions(cwd=cwd, env=env)})
                self.assertIsInstance(await get_session_history(session_id, cwd=cwd, env=env, agent_engine='v3'), list)
                await delete_session(session_id, cwd=cwd, env=env)
                self.assertNotIn(session_id, {s.session_id for s in await list_sessions(cwd=cwd, env=env)})
                session_id = None
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_generated_agent_permission_profile(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / 'workspace'
            cwd.mkdir()
            env = {'KIRO_HOME': str(Path(root) / 'kiro'),
                   'XDG_STATE_HOME': str(Path(root) / 'state'),
                   'XDG_CONFIG_HOME': str(Path(root) / 'config'),
                   'XDG_CACHE_HOME': str(Path(root) / 'cache')}
            name = 'sdk_permission_probe'
            definition = AgentDefinition('Restricted test agent', 'Reply briefly.', tools=['read'],
                                         kiro_permissions={'rules': [{'capability': 'shell', 'effect': 'deny'}]})
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine='v3',
                                           agent=name, agents={name: definition}, enable_kiro_hooks=False)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await client.list_kiro_permissions()
                    self.assertTrue(any(rule.get('scope') == 'agent' and rule.get('capability') == 'shell'
                                        and rule.get('effect') == 'deny' for rule in result.get('rules', [])))
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_policy_preset_new_resume_and_fork(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            session_id = None
            fork_id = None
            try:
                base = dict(cwd=cwd, env=env, agent_engine="v3", tools=[],
                            kiro_policy_presets=["read-workspace"], turn_timeout=90)
                async with KiroSDKClient(KiroAgentOptions(**base)) as client:
                    session_id = client.session_id
                    rules = (await client.list_kiro_permissions())["rules"]
                    self.assertTrue(any(rule.get("source") == "preset:read-workspace" for rule in rules))
                    await self._response(client, "Reply with OK only; do not use tools.")
                async with KiroSDKClient(KiroAgentOptions(**base, resume=session_id)) as client:
                    rules = (await client.list_kiro_permissions())["rules"]
                    self.assertTrue(any(rule.get("source") == "preset:read-workspace" for rule in rules))
                async with KiroSDKClient(KiroAgentOptions(**base, resume=session_id, fork_session=True)) as client:
                    fork_id = client.session_id
                    rules = (await client.list_kiro_permissions())["rules"]
                    self.assertTrue(any(rule.get("source") == "preset:read-workspace" for rule in rules))
            finally:
                for ident in (fork_id, session_id):
                    if ident:
                        await delete_session(ident, cwd=cwd, env=env)

    async def test_v3_prompt_hook_sets_session_title(self):
        import uuid

        title = "sdk-title-" + uuid.uuid4().hex[:10]
        async def hook(event, tool_id, context):
            return {'title': title}

        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=[],
                    hooks={"promptSubmit": [HookMatcher(hooks=[hook])]}, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await self._response(client, "Reply with OK only. Do not use tools.")
                    sessions = await list_sessions(cwd=cwd, env=env)
                    record = next(session for session in sessions if session.session_id == session_id)
                    self.assertEqual(record.title, title)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_context_categories_are_reported_values(self):
        import asyncio

        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=[], turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await self._response(client, "Reply OK without tools.")
                    for _ in range(50):
                        usage = await client.get_context_usage()
                        if usage and usage.get("categories"):
                            break
                        await asyncio.sleep(0.1)
                    raw = await client.get_context_usage_raw()
                    categories = usage["categories"]
                    breakdown = raw["_meta"]["kiro"]["breakdown"]
                    self.assertTrue(categories)
                    self.assertTrue(all(category["tokens"] == breakdown[category["name"]]["tokens"]
                                        for category in categories))
                    self.assertNotIn("totalTokens", usage)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_startup_fallback_uses_advertised_model(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=[], turn_timeout=90,
                model="kiro-sdk-unavailable-model", fallback_model="claude-sonnet-5.5",
                fallback_model_mode="startup")
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    self.assertTrue(any(item.get("id") == "model" and
                                        item.get("currentValue") == "claude-sonnet-5.5"
                                        for item in client.session_info.get("configOptions", [])))
                    await self._response(client, "Reply OK without tools.")
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_external_stdio_mcp_toggle(self):
        import asyncio
        from kiro_agent_sdk.external_mcp import ExternalStdioMcpBridge

        server_script = '''import json, os, pathlib, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        result = {"protocolVersion":"2025-11-25", "capabilities":{"tools":{}},
                  "serverInfo":{"name":"probe", "version":"1"}}
    elif msg.get("method") == "tools/list":
        result = {"tools":[{"name":"probe", "description":"Return the test marker",
                            "inputSchema":{"type":"object", "properties":{}}}]}
    elif msg.get("method") == "tools/call":
        path = pathlib.Path(os.environ["MCP_PROBE_LOG"])
        with path.open("a") as out:
            out.write("called\\n")
        result = {"content":[{"type":"text", "text":"EXTERNALPROBEOK"}]}
    else:
        print(json.dumps({"jsonrpc":"2.0", "id":msg["id"],
                          "error":{"code":-32601, "message":"Not found"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc":"2.0", "id":msg["id"], "result":result}), flush=True)
'''
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            script = Path(root) / "server.py"
            script.write_text(server_script)
            marker = Path(root) / "calls.txt"
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache")}
            observed = []

            async def gate(event, tool_id, context):
                name = event.get("tool_name", "")
                data = event.get("tool_input") or {}
                if ((name == "tool_load" and data.get("tool_ids") == ["probe_server::probe"]) or
                        ("probe_server" in name and "probe" in name)):
                    return {}
                return {'block': "Only the isolated probe is allowed"}

            options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"],
                mcp_servers={"probe_server": {"command": sys.executable, "args": [str(script)],
                    "env": {"MCP_PROBE_LOG": str(marker)}}}, sdk_mcp_toggle_mode="sdk-and-stdio",
                native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]}, permission_mode="trustAll",
                turn_timeout=90)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    for _ in range(150):
                        status = await client.get_mcp_status()
                        if any(item["name"] == "probe_server" and item["status"] == "connected"
                               for item in (status or {}).get("mcpServers", [])):
                            break
                        await asyncio.sleep(0.1)
                    else:
                        self.fail("External MCP server did not connect")
                    bridge = client._named_external_mcp_bridges["probe_server"][0]
                    def classify(line):
                        outcome = ExternalStdioMcpBridge._classify(line)
                        if outcome[0] and outcome[2] is not None:
                            observed.append(outcome[2])
                        return outcome
                    bridge._classify = classify
                    await client.toggle_mcp_server("probe_server", False)
                    await self._response(client, "Call the probe_server probe tool once. If it fails, stop and report "
                                                 "the error. Do not use any other tools.")
                    self.assertTrue(observed, "Kiro did not attempt the disabled tool")
                    self.assertFalse(marker.exists())
                    await client.toggle_mcp_server("probe_server", True)
                    result = await self._response(client, "Call the probe_server probe tool once and report its result. "
                                                         "Do not use any other tools.")
                    self.assertEqual(marker.read_text().splitlines(), ["called"])
                    self.assertIn("EXTERNALPROBEOK", result.result.upper())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_remote_mcp_toggle(self):
        import asyncio
        from kiro_agent_sdk.remote_mcp import RemoteMcpBridge
        from test_remote_mcp_toggle import TestMcpHttpServer
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("optional mcp dependency not installed")

        for kind in ("http", "sse"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                service = TestMcpHttpServer(kind)
                url = await service.start()
                cwd = Path(root) / "workspace"
                cwd.mkdir()
                env = {"KIRO_HOME": str(Path(root) / "kiro"),
                       "XDG_STATE_HOME": str(Path(root) / "state"),
                       "XDG_CONFIG_HOME": str(Path(root) / "config"),
                       "XDG_CACHE_HOME": str(Path(root) / "cache")}
                observed = []

                async def gate(event, tool_id, context):
                    name = event.get("tool_name", "")
                    data = event.get("tool_input") or {}
                    if ((name == "tool_load" and data.get("tool_ids") == ["probe_server::probe"]) or
                            ("probe_server" in name and "probe" in name)):
                        return {}
                    return {'block': "Only the isolated probe is allowed"}

                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"],
                    mcp_servers={"probe_server": {"type": kind, "url": url,
                        "headers": {"Authorization": "Bearer fixture-secret"}}},
                    sdk_mcp_toggle_mode="sdk-all", native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                    permission_mode="trustAll", turn_timeout=90)
                session_id = None
                try:
                    async with KiroSDKClient(options) as client:
                        session_id = client.session_id
                        for _ in range(150):
                            status = await client.get_mcp_status()
                            if any(item["name"] == "probe_server" and item["status"] == "connected"
                                   for item in (status or {}).get("mcpServers", [])):
                                break
                            await asyncio.sleep(0.1)
                        else:
                            self.fail(f"{kind} MCP server did not connect")
                        bridge = client._named_external_mcp_bridges["probe_server"][0]
                        def classify(line):
                            outcome = RemoteMcpBridge._classify(line)
                            if outcome[0] and outcome[2] is not None:
                                observed.append(outcome[2])
                            return outcome
                        bridge._classify = classify
                        await client.toggle_mcp_server("probe_server", False)
                        await self._response(client, "Call the probe_server probe tool once. If it fails, stop and "
                                                     "report the error. Do not use any other tools.")
                        self.assertTrue(observed, f"Kiro did not attempt the disabled {kind} tool")
                        self.assertEqual(service.calls, [])
                        await client.toggle_mcp_server("probe_server", True)
                        result = await self._response(client, "Call the probe_server probe tool once and report its "
                                                             "result. Do not use any other tools.")
                        self.assertEqual(service.calls, ["probe"])
                        self.assertIn("REMOTE-RESULT", result.result.upper())
                        self.assertFalse(service.bad_auth)
                finally:
                    try:
                        if session_id:
                            await delete_session(session_id, cwd=cwd, env=env)
                    finally:
                        await service.close()

    async def test_v3_remote_mcp_cancellation(self):
        await self._test_v3_remote_mcp_cancellation("http")

    async def test_v3_sse_remote_mcp_cancellation(self):
        await self._test_v3_remote_mcp_cancellation("sse")

    async def test_v3_interrupt_cancels_observed_http_mcp(self):
        await self._test_v3_remote_mcp_cancellation("http", interrupt=True)

    async def test_v3_interrupt_cancels_observed_sse_mcp(self):
        await self._test_v3_remote_mcp_cancellation("sse", interrupt=True)

    async def _test_v3_remote_mcp_cancellation(self, kind, *, interrupt=False):
        import asyncio
        from test_remote_mcp_toggle import TestMcpHttpServer
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("optional mcp dependency not installed")
        service = TestMcpHttpServer(kind, protocol_version="2025-03-26")
        service.block_next = True
        service.stall_sse_tool_post = kind == "sse"
        url = await service.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                cwd = Path(root) / "workspace"
                cwd.mkdir()
                env = {"KIRO_HOME": str(Path(root) / "kiro"),
                       "XDG_STATE_HOME": str(Path(root) / "state"),
                       "XDG_CONFIG_HOME": str(Path(root) / "config"),
                       "XDG_CACHE_HOME": str(Path(root) / "cache")}
                async def gate(event, tool_id, context):
                    name = event.get("tool_name", "")
                    data = event.get("tool_input") or {}
                    if ((name == "tool_load" and data.get("tool_ids") == ["probe_server::probe"]) or
                            ("probe_server" in name and "probe" in name)):
                        return {}
                    return {'block': "Only the isolated probe is allowed"}
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"],
                    mcp_servers={"probe_server": {"type": kind, "url": url,
                        "headers": {"Authorization": "Bearer fixture-secret"}}},
                    sdk_mcp_toggle_mode="sdk-all",
                    sdk_tool_interrupt_mode="cancel-observed" if interrupt else "kiro",
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                    permission_mode="trustAll", turn_timeout=90)
                session_id = None
                response_task = None
                try:
                    async with KiroSDKClient(options) as client:
                        session_id = client.session_id
                        for _ in range(150):
                            status = await client.get_mcp_status()
                            if any(item["name"] == "probe_server" and item["status"] == "connected"
                                   for item in (status or {}).get("mcpServers", [])):
                                break
                            await asyncio.sleep(0.1)
                        else:
                            self.fail("Remote MCP server did not connect")
                        await client.query("Call the probe_server probe tool once and report its result. "
                                           "Do not use any other tools.")
                        async def receive():
                            return [item async for item in client.receive_response()]
                        response_task = asyncio.create_task(receive())
                        await asyncio.wait_for(service.started.wait(), 60)
                        if interrupt:
                            await client.interrupt()
                        else:
                            await client.request_mcp_tool_cancellation("probe_server")
                        for _ in range(200):
                            if service.cancellations:
                                break
                            await asyncio.sleep(0.01)
                        messages = await asyncio.wait_for(response_task, 60)
                        self.assertTrue(service.cancellations)
                        self.assertEqual(service.calls, ["probe"])
                        self.assertTrue(any(isinstance(item, ResultMessage) for item in messages))
                        self.assertFalse(service.bad_auth)
                finally:
                    service.release.set()
                    if response_task is not None and not response_task.done():
                        response_task.cancel()
                        await asyncio.gather(response_task, return_exceptions=True)
                    if session_id:
                        await delete_session(session_id, cwd=cwd, env=env)
        finally:
            await service.close()

    async def test_v3_mcp_config_expands_environment(self):
        import asyncio

        script = '''import json, os, pathlib, sys
pathlib.Path(os.environ["PROBE_OUT"]).write_text(json.dumps(
    {"value": os.environ["PROBE_VALUE"], "pwd": os.environ["PROBE_PWD"]}))
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "probe", "version": "1"}}
    elif msg.get("method") == "tools/list":
        result = {"tools": []}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                          "error": {"code": -32601, "message": "Not found"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
'''
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            server_script = Path(root) / "server.py"
            server_script.write_text(script)
            output = Path(root) / "observed.txt"
            config = {"mcpServers": {"json_probe": {
                "command": "${PROBE_PYTHON}", "args": ["${PROBE_SCRIPT}"],
                "env": {"PROBE_OUT": "${PROBE_OUT}", "PROBE_VALUE": "${PROBE_VALUE:-fallback}",
                        "PROBE_PWD": "${PWD}"}}}}
            (cwd / "mcp.json").write_text(json.dumps(config))
            env = {"KIRO_HOME": str(Path(root) / "kiro"),
                   "XDG_STATE_HOME": str(Path(root) / "state"),
                   "XDG_CONFIG_HOME": str(Path(root) / "config"),
                   "XDG_CACHE_HOME": str(Path(root) / "cache"),
                   "PROBE_PYTHON": sys.executable, "PROBE_SCRIPT": str(server_script),
                   "PROBE_OUT": str(output), "PROBE_VALUE": "literal-${PROBE_SECOND}",
                   "PROBE_SECOND": "unexpected", "PWD": "/stale"}
            for source in ("file", "dictionary"):
                with self.subTest(source=source):
                    output.unlink(missing_ok=True)
                    session_id = None
                    try:
                        options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"],
                            mcp_servers=Path("mcp.json") if source == "file" else config["mcpServers"],
                            mcp_env_mode="literal" if source == "file" else "expand")
                        async with KiroSDKClient(options) as client:
                            session_id = client.session_id
                            for _ in range(150):
                                status = await client.get_mcp_status()
                                if any(server["name"] == "json_probe" and server["status"] == "connected"
                                       for server in (status or {}).get("mcpServers", [])):
                                    break
                                await asyncio.sleep(0.1)
                            else:
                                self.fail(f"MCP server did not connect: {status!r}; output exists: {output.exists()}")
                            self.assertEqual(json.loads(output.read_text()),
                                             {"value": "literal-${PROBE_SECOND}", "pwd": str(cwd.resolve())})
                    finally:
                        if session_id:
                            await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_selected_agent_initial_prompt_and_resume(self):
        import uuid

        name = 'sdk_initial_' + uuid.uuid4().hex[:8]
        marker = 'MEMO' + uuid.uuid4().hex[:10].upper()
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / 'workspace'
            cwd.mkdir()
            env = {'KIRO_HOME': str(Path(root) / 'kiro'),
                   'XDG_STATE_HOME': str(Path(root) / 'state'),
                   'XDG_CONFIG_HOME': str(Path(root) / 'config'),
                   'XDG_CACHE_HOME': str(Path(root) / 'cache')}
            definition = AgentDefinition('Initial prompt test', 'Follow the user instructions.', tools=[],
                                         initial_prompt=f'Remember this exact token: {marker}. Reply INITIALREADY.')
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine='v3',
                                           agent=name, agents={name: definition}, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query('Return only the token I asked you to remember.')
                    messages = [message async for message in client.receive_messages()]
                    responses = [message.result for message in messages if isinstance(message, ResultMessage)]
                    self.assertEqual(len(responses), 2)
                    self.assertIn('INITIALREADY', responses[0].upper())
                    self.assertIn(marker, responses[1].upper())
                options.resume = session_id
                async with KiroSDKClient(options) as resumed:
                    await resumed.query('Return only the token I asked you to remember.')
                    messages = [message async for message in resumed.receive_messages()]
                    responses = [message.result for message in messages if isinstance(message, ResultMessage)]
                    self.assertEqual(len(responses), 1)
                    self.assertIn(marker, responses[0].upper())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v2_selected_agent_initial_prompt(self):
        import uuid

        name = 'sdk_initial_' + uuid.uuid4().hex[:8]
        marker = 'MEMO' + uuid.uuid4().hex[:10].upper()
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / 'workspace'
            cwd.mkdir()
            env = {'KIRO_HOME': str(Path(root) / 'kiro'),
                   'XDG_STATE_HOME': str(Path(root) / 'state'),
                   'XDG_CONFIG_HOME': str(Path(root) / 'config'),
                   'XDG_CACHE_HOME': str(Path(root) / 'cache')}
            definition = AgentDefinition('Initial prompt test', 'Follow the user instructions.', tools=[],
                                         initial_prompt=f'Remember this exact token: {marker}. Reply INITIALREADY.')
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine='v2',
                                           agent=name, agents={name: definition}, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query('Return only the token I asked you to remember.')
                    messages = [message async for message in client.receive_messages()]
                    responses = [message.result for message in messages if isinstance(message, ResultMessage)]
                    self.assertEqual(len(responses), 2)
                    self.assertIn('INITIALREADY', responses[0].upper())
                    self.assertIn(marker, responses[1].upper())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v2_v3_generated_agent_preloads_local_skill(self):
        import uuid
        with tempfile.TemporaryDirectory() as cwd:
            skill = Path(cwd) / '.kiro' / 'skills' / 'sdk-probe' / 'SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: sdk-probe\ndescription: Test skill\n---\n'
                             'The requested marker is SKILLMAGENTA937.\n')
            for engine in ('v2', 'v3'):
                with self.subTest(engine=engine):
                    name = 'sdk_skill_probe_' + uuid.uuid4().hex[:8]
                    definition = AgentDefinition('Test profile', 'Answer using the provided resources.',
                                                 tools=[], skills=['sdk-probe'])
                    session_id = None
                    try:
                        options = KiroAgentOptions(cwd=cwd, agent_engine=engine, agent=name,
                                                   agents={name: definition}, turn_timeout=90)
                        async with KiroSDKClient(options) as client:
                            session_id = client.session_id
                            result = await self._response(client, 'Report the marker in the provided resource. '
                                                                  'Do not use tools.')
                            self.assertIn('SKILLMAGENTA937', result.result.upper())
                    finally:
                        if session_id:
                            await delete_session(session_id, cwd=cwd)

    async def test_v3_store_records_the_title_kiro_reports(self):
        store = InMemorySessionStore()
        with tempfile.TemporaryDirectory() as cwd:
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=[], session_store=store, turn_timeout=90)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await self._response(client, "Write a haiku about release notes. Do not use tools.")
                    await self._response(client, "Now make it rhyme. Do not use tools.")
                    generated = next(s for s in await list_sessions(cwd=cwd) if s.session_id == session_id)
                    recorded = (await list_sessions_from_store(store, directory=cwd))[0]
                    self.assertTrue(generated.title)
                    self.assertEqual(recorded.title, generated.title)
                    await client.rename_session("Release Haiku")
                kiro = next(s for s in await list_sessions(cwd=cwd) if s.session_id == session_id)
                stored = (await list_sessions_from_store(store, directory=cwd))[0]
                self.assertEqual((stored.session_id, kiro.title, stored.title), (session_id, "Release Haiku", "Release Haiku"))
                self.assertEqual(stored.cwd, str(Path(cwd).resolve()))
                self.assertIsInstance(stored.updated_at, int)
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_session_archive_records_and_message_ids(self):
        store = InMemorySessionStore()
        with tempfile.TemporaryDirectory() as cwd:
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, agent_engine='v3', tools=[],
                                           session_store=store, session_store_archive=True)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, 'Remember ORANGE849. Reply OK without tools.')
                preview = await get_session_archive_messages(session_id, directory=cwd)
                users = [m for m in preview if isinstance(m, UserMessage)]
                self.assertTrue(any('ORANGE849' in str(m.content) for m in users))
                self.assertTrue(any(m.message_id == result.user_message_id for m in users))
                stored = await get_session_archive_messages_from_store(store, session_id, directory=cwd)
                self.assertEqual(stored, preview)
                records = await get_session_archive_history_from_store(store, session_id, directory=cwd)
                self.assertTrue(any(record['payload'].get('type') == 'user' for record in records))
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    @unittest.skipUnless(shutil.which('git'), 'git is required')
    async def test_list_git_worktrees_and_all_directories(self):
        with tempfile.TemporaryDirectory() as root:
            origin, branch = Path(root) / 'origin', Path(root) / 'branch'
            origin.mkdir()
            def git(*args):
                subprocess.run(['git', *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            git('init', '-q', str(origin))
            git('-C', str(origin), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                '-c', f'core.hooksPath={os.devnull}', 'commit', '-q', '--allow-empty', '-m', 'initial')
            git('-C', str(origin), 'worktree', 'add', '-q', '-b', 'sdk-test', str(branch))
            created = []
            try:
                for cwd in (origin, branch):
                    async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine='v3')) as client:
                        created.append((cwd, client.session_id))
                        await self._response(client, 'Reply OK without tools.')
                expected = {sid for _, sid in created}
                scoped = await list_sessions(directory=origin, include_worktrees=True)
                self.assertTrue(expected.issubset({item.session_id for item in scoped}))
                self.assertEqual({item.cwd for item in scoped if item.session_id in expected},
                                 {str(origin), str(branch)})
                global_ids = {item.session_id for item in await list_sessions(all_directories=True)}
                self.assertTrue(expected.issubset(global_ids))
            finally:
                for cwd, session_id in created:
                    await delete_session(session_id, cwd=cwd)

    async def test_v2_v3_mcp_server_status(self):
        import asyncio
        @tool('ping', 'Return pong', {})
        async def ping(args):
            return {'content': [{'type': 'text', 'text': 'pong'}]}
        with tempfile.TemporaryDirectory() as cwd:
            for engine in ('v2', 'v3'):
                with self.subTest(engine=engine):
                    session_id = None
                    try:
                        servers = {name: create_mcp_server(name, tools=[ping])
                                   for name in ('sdk_first', 'sdk_second')}
                        options = KiroAgentOptions(cwd=cwd, agent_engine=engine, mcp_servers=servers)
                        async with KiroSDKClient(options) as client:
                            session_id = client.session_id
                            status = None
                            for _ in range(200):
                                status = await client.get_mcp_status()
                                connected = {server['name'] for server in (status or {}).get('mcpServers', [])
                                             if server['status'] == 'connected'}
                                if {'sdk_first', 'sdk_second'} <= connected:
                                    break
                                await asyncio.sleep(0.1)
                            self.assertTrue({'sdk_first', 'sdk_second'} <= connected, status)
                            self.assertIsNotNone(await client.get_mcp_status_raw())
                    finally:
                        if session_id:
                            await delete_session(session_id, cwd=cwd)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "requires MCP")
    async def test_v3_preauthorize_oauth_before_kiro_connection(self):
        import asyncio
        from kiro_agent_sdk import McpOAuthConfig, preauthorize_mcp_oauth
        from test_mcp_oauth import OAuthFixture

        fixture = OAuthFixture("http")
        url = await fixture.start()
        calls = []

        async def authorize(authorization_url):
            calls.append("started")
            await asyncio.sleep(35)
            calls.append("completed")
            return await fixture.authorize(authorization_url)

        oauth = McpOAuthConfig("http://127.0.0.1:9999/callback", authorize)
        server = {"type": "http", "url": url}
        try:
            await preauthorize_mcp_oauth(server, oauth, timeout=90)
            self.assertEqual(calls, ["started", "completed"])
            self.assertEqual(fixture.token_grants, ["authorization_code"])
            with tempfile.TemporaryDirectory() as cwd:
                async def gate(event, tool_id, context):
                    name, data = event.get("tool_name", ""), event.get("tool_input") or {}
                    allowed = ((name == "tool_load" and data.get("tool_ids") == ["oauth_probe::probe"]) or
                               ("oauth_probe" in name and "probe" in name))
                    return {} if allowed else {"block": "Only the test tool is allowed"}

                options = KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=["@mcp"], turn_timeout=90,
                    permission_mode="trustAll", sdk_mcp_toggle_mode="sdk-all",
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                    mcp_servers={"oauth_probe": server}, mcp_oauth={"oauth_probe": oauth})
                session_id = None
                try:
                    async with KiroSDKClient(options) as client:
                        session_id = client.session_id
                        for _ in range(100):
                            status = await client.get_mcp_status()
                            if any(s.get("name") == "oauth_probe" and s.get("status") == "connected"
                                   for s in (status or {}).get("mcpServers", [])):
                                break
                            await asyncio.sleep(.2)
                        else:
                            self.fail("Preauthorized MCP server did not connect")
                        result = await self._response(client, "Call the oauth_probe probe tool once and report its "
                                                              "result. Do not use any other tools.")
                        self.assertIn("OAUTH_PROBE_OK", result.result.upper())
                        self.assertEqual(fixture.calls, ["probe"])
                        self.assertEqual(calls, ["started", "completed"])
                        self.assertEqual(fixture.token_grants, ["authorization_code"])
                finally:
                    if session_id:
                        await delete_session(session_id, cwd=cwd)
        finally:
            await fixture.close()

    async def test_v3_mcp_form_elicitation(self):
        import asyncio
        import uuid
        try:
            import mcp.types as types
            from mcp.server import Server
        except ImportError:
            self.skipTest('optional mcp dependency not installed')
        if hasattr(types, 'jsonrpc_message_adapter'):
            self.skipTest('the MCP 2026 protocol does not allow server-initiated requests')
        suffix = uuid.uuid4().hex[:8]
        server_name, tool_name = 'sdk_survey_' + suffix, 'sdk_elicit_' + suffix
        server = Server(server_name)

        @server.list_tools()
        async def list_tools():
            return [types.Tool(name=tool_name, description='Ask the user for their favorite color.',
                               inputSchema={'type': 'object', 'properties': {}})]

        @server.call_tool()
        async def call_tool(name, arguments):
            response = await server.request_context.session.elicit(
                'Choose a color', {'type': 'object', 'properties': {'color': {'type': 'string'}},
                                   'required': ['color']})
            return [types.TextContent(type='text', text=str(response.content or response.action))]

        received = []
        async def elicit(request):
            received.append(request)
            return {'action': 'accept', 'content': {'color': 'PURPLE'}}

        async def gate(event, tool_id, context):
            name = event.get('tool_name', '')
            data = json.dumps(event.get('tool_input') or {}, ensure_ascii=False)
            if any(token in name or token in data for token in (server_name, tool_name)):
                return {}
            return {'block': 'Only the isolated test tool is allowed'}

        with tempfile.TemporaryDirectory() as cwd:
            session_id = None
            try:
                options = KiroAgentOptions(
                    cwd=cwd, agent_engine='v3', tools=['@mcp'],
                    native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                    mcp_servers={server_name: {'type': 'sdk', 'name': server_name, 'instance': server}},
                    permission_mode='trustAll', on_mcp_elicitation=elicit, turn_timeout=100)
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    for _ in range(120):
                        status = await client.get_mcp_status()
                        if any(s['name'] == server_name and s['status'] == 'connected'
                               for s in (status or {}).get('mcpServers', [])):
                            break
                        await asyncio.sleep(.1)
                    else:
                        self.fail('Test MCP server did not connect')
                    result = await self._response(
                        client, f'Call the {tool_name} tool, wait for its response, and report the color. '
                                'Do not use other tools.')
                    self.assertEqual(len(received), 1)
                    self.assertEqual(received[0]['elicitation']['mode'], 'form')
                    self.assertIn('PURPLE', result.result.upper())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_native_hook_survives_unrelated_hook_change(self):
        import asyncio
        async def hook(event, tool_id, context):
            return {}
        with tempfile.TemporaryDirectory() as cwd:
            folder = Path(cwd) / '.kiro' / 'hooks'
            folder.mkdir(parents=True)
            path = folder / 'probe.json'
            def write(name):
                path.write_text(json.dumps({'version': 'v1', 'hooks': [
                    {'name': name, 'trigger': 'PostFileSave', 'action': {'type': 'command', 'command': '/bin/true'}}]}))
            write('sdk_probe_before')
            session_id = None
            try:
                options = KiroAgentOptions(cwd=cwd, agent_engine='v3', tools=[],
                                           native_hook_bridge_mode="same-user", native_hooks={'preToolUse': [HookMatcher(hooks=[hook])]})
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    while not client._events.empty():
                        client._events.get_nowait()
                    write('sdk_probe_after')
                    async def changed():
                        while True:
                            event = await client._events.get()
                            if isinstance(event, dict) and event.get('method') == '_kiro/hooks/didChange':
                                names = [item.get('name') for item in event.get('params', {}).get('hooks', [])]
                                if 'sdk_probe_after' in names:
                                    return names
                    names = await asyncio.wait_for(changed(), 10)
                    self.assertTrue(any(isinstance(name, str) and name.startswith('kiro-sdk-') for name in names))
                    self.assertTrue((await client.extension_request('_kiro/hooks/list', {'sessionId': session_id}))['hooks'])
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v2_and_v3_query_and_resume(self):
        with tempfile.TemporaryDirectory() as cwd:
            for engine in ('v2', 'v3'):
                with self.subTest(engine=engine):
                    session_id = None
                    try:
                        async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine=engine)) as client:
                            session_id = client.session_id
                            await self._response(client, 'Reply with OK only; do not use tools.')
                        async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine=engine, resume=session_id)) as client:
                            self.assertEqual(client.session_id, session_id)
                            self.assertTrue(any(e.get('sessionUpdate') == 'agent_message_chunk' for e in client.history))
                            await self._response(client, 'Reply with OK again; do not use tools.')
                    finally:
                        if session_id:
                            await delete_session(session_id, cwd=cwd)

    async def test_v3_fork_at_user_message(self):
        with tempfile.TemporaryDirectory() as cwd:
            fork_id = None
            parent_id = None
            try:
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine='v3')) as client:
                    parent_id = client.session_id
                    ids = []
                    for text in ('Remember ALPHA. Reply OK.', 'Remember BETA. Reply OK.'):
                        await client.query(text)
                        messages = [m async for m in client.receive_response()]
                        ids.extend(m.data.get('_meta', {}).get('kiro', {}).get('userMessageId')
                                   for m in messages if isinstance(m, KiroEvent)
                                   and m.data.get('_meta', {}).get('kiro', {}).get('userMessageId'))
                    self.assertEqual(len(ids), 2)
                    fork_id = (await client.fork_session(message_id=ids[0]))['sessionId']
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine='v3', resume=fork_id)) as fork:
                    prompts = [event.get('content', {}).get('text', '') for event in fork.history
                               if event.get('sessionUpdate') == 'user_message_chunk']
                    self.assertTrue(any('ALPHA' in p for p in prompts))
                    self.assertFalse(any('BETA' in p for p in prompts))
            finally:
                if fork_id:
                    await delete_session(fork_id, cwd=cwd)
                if parent_id:
                    await delete_session(parent_id, cwd=cwd)

    async def test_v3_resume_fork_at_archived_assistant_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            parent_id = fork_id = None
            try:
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=[])) as client:
                    parent_id = client.session_id
                    await self._response(client, "Remember CEDAR941. Reply only OK; do not use tools.")
                    records = await get_session_archive_history(parent_id, directory=cwd, env=env)
                    assistants = [item for item in records if item.get("payload", {}).get("type") == "assistant"
                                  and item.get("payload", {}).get("operationType") in (None, "Say")
                                  and isinstance(item.get("id"), str)]
                    self.assertTrue(assistants)
                    keep = assistants[-1]["id"]
                    await self._response(client, "Remember COTTON832. Reply only OK; do not use tools.")
                options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=[], resume=parent_id,
                                           fork_session=True, resume_session_at=keep)
                async with KiroSDKClient(options) as branch:
                    fork_id = branch.session_id
                    self.assertNotEqual(fork_id, parent_id)
                    replay = [event for event in branch.history if isinstance(event, dict)]
                    prompts = [event.get("content", {}).get("text", "") for event in replay
                               if event.get("sessionUpdate") == "user_message_chunk"]
                    answers = [event.get("content", {}).get("text", "") for event in replay
                               if event.get("sessionUpdate") == "agent_message_chunk"]
                    self.assertTrue(any("CEDAR941" in text for text in prompts))
                    self.assertFalse(any("COTTON832" in text for text in prompts))
                    self.assertTrue(any("OK" in text.upper() for text in answers))
            finally:
                if fork_id:
                    await delete_session(fork_id, cwd=cwd, env=env)
                if parent_id:
                    await delete_session(parent_id, cwd=cwd, env=env)

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "agentcore extra")
    async def test_v3_agentcore_archive_heavy_bounded_history(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        from test_agentcore import HEADER, request
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd_env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                       "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            store = SQLiteSessionStore(root / "mirror.db")
            async def owner(req, session):
                return req.headers.get("x-test-owner") == "yes" and session == "history-live"
            app = create_agentcore_app(
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "app", auth_mode="cli", allow_session_history=True),
                options_factory=lambda workspace, resume: KiroAgentOptions(
                    env=cwd_env, tools=[], session_store=store, session_store_archive=True),
                session_authorizer=owner)
            headers = [(HEADER, b"history-live"), (b"x-test-owner", b"yes")]
            session_id = cwd = None
            try:
                status, body, _ = await request(
                    app, "POST", "/invocations", json.dumps({"prompt": "Reply only GREEN; do not use tools."}).encode(),
                    headers, timeout=120)
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(body)["is_error"])
                client = app.state.kiro_agentcore.sessions["history-live"].client
                session_id, cwd = client.session_id, client.options.cwd
                key = dict(client._store_key)
                entries = await store.load(key)
                archive = next(entry for entry in entries if entry.get("type") == "session_archive")
                archive_size = len(json.dumps(archive, ensure_ascii=False).encode("utf-8"))
                copies = 9 * 1024 * 1024 // archive_size + 1
                self.assertLessEqual(copies, 12000)
                await store.append(key, [archive] * copies)
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=20000, max_bytes=8 * 1024 * 1024)
                messages = await get_session_messages_from_store(
                    store, session_id, directory=cwd, bounded_without_archives=True)
                self.assertTrue(messages[0].content[0].text.startswith("Reply only GREEN"))
                status, body, _ = await request(
                    app, "POST", "/invocations", json.dumps({"action": "session_messages"}).encode(),
                    headers, timeout=120)
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(body)["is_error"])
                self.assertIn(b"Reply only GREEN", body)
                self.assertNotIn(b"base64", body)
            finally:
                await app.state.kiro_agentcore.close()
                if session_id:
                    await delete_session(session_id, directory=cwd, env=cwd_env)

    async def test_v3_sessionless_rename_and_fork(self):
        with tempfile.TemporaryDirectory() as cwd:
            try:
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine='v3')) as client:
                    source = client.session_id
                    result = await self._response(client, 'Remember ORCHID621. Reply OK; do not use tools.')
                    self.assertTrue(result.user_message_id)
                before = await list_sessions(cwd=cwd)
                await rename_session(source, 'control renamed', cwd=cwd)
                after_rename = await list_sessions(cwd=cwd)
                self.assertEqual({s['sessionId'] for s in before}, {s['sessionId'] for s in after_rename})
                info = next(s for s in after_rename if s.session_id == source)
                self.assertEqual(info.title, 'control renamed')
                self.assertIsInstance(info.updated_at, int)
                fork = await fork_session(source, cwd=cwd, up_to_message_id=result.user_message_id,
                                          title='control fork')
                self.assertNotEqual(fork.session_id, source)
                sessions = await list_sessions(cwd=cwd)
                self.assertEqual({s['sessionId'] for s in sessions}, {source, fork.session_id})
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine='v3', resume=fork.session_id)) as branch:
                    prompts = [event.get('content', {}).get('text', '') for event in branch.history
                               if event.get('sessionUpdate') == 'user_message_chunk']
                    self.assertTrue(any('ORCHID621' in text for text in prompts))
            finally:
                for session in await list_sessions(cwd=cwd):
                    if session.get('sessionId'):
                        await delete_session(session['sessionId'], cwd=cwd)

    async def test_v3_store_only_fork_at_message(self):
        with tempfile.TemporaryDirectory() as root:
            origin, target = Path(root) / 'origin', Path(root) / 'target'
            origin.mkdir()
            target.mkdir()
            store = SQLiteSessionStore(Path(root) / 'mirror.sqlite3')
            try:
                async with KiroSDKClient(KiroAgentOptions(cwd=origin, agent_engine='v3', tools=[])) as client:
                    source = client.session_id
                    first = await self._response(client, 'Remember FERN826. Reply OK; do not use tools.')
                    self.assertTrue(first.user_message_id)
                    await self._response(client, 'Remember POPPY917. Reply OK; do not use tools.')
                await import_session_to_store(source, store, directory=origin, batch_size=2)
                before = {s['sessionId'] for s in await list_sessions(cwd=origin)}
                branch = await fork_session_via_store(store, source, directory=origin,
                                                       up_to_message_id=first.user_message_id,
                                                       title='stored fork', batch_size=2, timeout=60,
                                                       bounded_mirror_read=True)
                after = {s['sessionId'] for s in await list_sessions(cwd=origin)}
                self.assertEqual(after, before)
                info = await get_session_info_from_store(store, branch.session_id, origin)
                self.assertEqual(info.title, 'stored fork')
                options = KiroAgentOptions(cwd=target, agent_engine='v3', tools=[], resume=branch.session_id,
                                           session_store=store, resume_from_store=True,
                                           session_store_source_directory=origin, resume_from_store_bounded_read=True)
                async with KiroSDKClient(options) as client:
                    prompts = [event.get('content', {}).get('text', '') for event in client.history
                               if event.get('sessionUpdate') == 'user_message_chunk']
                    self.assertTrue(any('FERN826' in text for text in prompts))
                    self.assertFalse(any('POPPY917' in text for text in prompts))
            finally:
                for cwd in (origin, target):
                    for session in await list_sessions(cwd=cwd):
                        if session.get('sessionId'):
                            await delete_session(session['sessionId'], cwd=cwd)

    async def test_v3_resume_from_archive_heavy_mirror_with_bounded_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir()
            target.mkdir()
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            store = SQLiteSessionStore(root / "mirror.db")
            parent_id = restored_id = None
            try:
                options = KiroAgentOptions(cwd=source, env=env, agent_engine="v3", tools=[],
                                           session_store=store, session_store_archive=True)
                async with KiroSDKClient(options) as client:
                    parent_id = client.session_id
                    await self._response(client, "Remember the marker ORCHID529. Reply OK; do not use tools.")
                    key = dict(client._store_key)
                entries = await store.load(key)
                archive = next(entry for entry in reversed(entries) if entry.get("type") == "session_archive")
                archive_size = len(json.dumps(archive, ensure_ascii=False).encode("utf-8"))
                copies = 9 * 1024 * 1024 // archive_size + 1
                self.assertLessEqual(copies, 12000)
                await store.append(key, [archive] * copies)
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=20000, max_bytes=8 * 1024 * 1024)
                options = KiroAgentOptions(
                    cwd=target, env=env, agent_engine="v3", tools=[], resume=parent_id, session_store=store,
                    resume_from_store=True, resume_from_store_bounded_read=True,
                    session_store_source_directory=source)
                async with KiroSDKClient(options) as restored:
                    restored_id = restored.restored_session_id
                    self.assertTrue(restored_id)
                    self.assertNotEqual(restored_id, parent_id)
                    prompts = [event.get("content", {}).get("text", "") for event in restored.history
                               if event.get("sessionUpdate") == "user_message_chunk"]
                    self.assertTrue(any("ORCHID529" in text for text in prompts))
                    result = await self._response(restored, "Return the saved marker exactly; do not use tools.")
                    self.assertIn("ORCHID529", result.result.upper())
            finally:
                if restored_id:
                    await delete_session(restored_id, cwd=target, env=env)
                if parent_id:
                    await delete_session(parent_id, cwd=source, env=env)

    @unittest.skipUnless(importlib.util.find_spec("starlette"), "agentcore extra")
    async def test_v3_agentcore_restarts_using_bounded_archive_recovery(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        from test_agentcore import HEADER, request
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            store = SQLiteSessionStore(root / "mirror.db")
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "app", auth_mode="cli")
            async def owner(req, session):
                return req.headers.get("x-test-owner") == "yes" and session == "recovery-live"
            def options(workspace, resume):
                return KiroAgentOptions(
                    env=env, tools=[], session_store=store, session_store_archive=True,
                    resume_from_store=resume is not None, resume_from_store_bounded_read=resume is not None)
            apps = []
            workspace = None
            try:
                app = create_agentcore_app(config, options_factory=options, session_authorizer=owner)
                apps.append(app)
                headers = [(HEADER, b"recovery-live"), (b"x-test-owner", b"yes")]
                status, body, _ = await request(
                    app, "POST", "/invocations",
                    json.dumps({"prompt": "Remember QUARTZ842. Reply OK; do not use tools."}).encode(),
                    headers, timeout=120)
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(body)["is_error"])
                original = app.state.kiro_agentcore.sessions["recovery-live"].client
                original_id, workspace, key = original.session_id, original.options.cwd, dict(original._store_key)
                await app.state.kiro_agentcore.close()
                entries = await store.load(key)
                archive = next(entry for entry in reversed(entries) if entry.get("type") == "session_archive")
                archive_size = len(json.dumps(archive, ensure_ascii=False).encode("utf-8"))
                copies = 9 * 1024 * 1024 // archive_size + 1
                self.assertLessEqual(copies, 12000)
                await store.append(key, [archive] * copies)
                app = create_agentcore_app(config, options_factory=options, session_authorizer=owner)
                apps.append(app)
                status, _, _ = await request(app, "POST", "/invocations", b'{"prompt":"hello"}',
                                             [(HEADER, b"recovery-live"), (b"x-test-owner", b"no")], timeout=120)
                self.assertEqual(status, 403)
                status, body, _ = await request(
                    app, "POST", "/invocations",
                    json.dumps({"prompt": "Return the saved marker exactly; do not use tools."}).encode(),
                    headers, timeout=120)
                self.assertEqual(status, 200)
                response = json.loads(body)
                self.assertFalse(response["is_error"])
                self.assertIn("QUARTZ842", response["response"].upper())
                client = app.state.kiro_agentcore.sessions["recovery-live"].client
                self.assertTrue(client.restored_session_id)
                self.assertNotEqual(client.session_id, original_id)
            finally:
                for app in apps:
                    await app.state.kiro_agentcore.close()
                if workspace is not None:
                    for item in await list_sessions(cwd=workspace, env=env):
                        if item.get("sessionId"):
                            await delete_session(item["sessionId"], cwd=workspace, env=env)

    async def test_v3_mirrored_user_ids_can_restore_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            database = Path(root) / "mirror.sqlite3"
            store = SQLiteSessionStore(database)
            options = KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=[], turn_timeout=90,
                                       enable_file_checkpointing=True, session_store=store)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    first = await self._response(client, "Reply FIRST only. Do not use tools.")
                    second = await self._response(client, "Reply SECOND only. Do not use tools.")
                    stored = await get_session_messages_from_store(
                        SQLiteSessionStore(database), session_id, directory=cwd)
                    users = [item for item in stored if isinstance(item, UserMessage)]
                    self.assertEqual(len(users), 2)
                    self.assertTrue(users[0].message_id)
                    self.assertNotEqual(users[0].message_id, users[1].message_id)
                    self.assertEqual([item.message_id for item in users],
                                     [first.user_message_id, second.user_message_id])
                    self.assertEqual([item.content for item in users],
                                     [[TextBlock("Reply FIRST only. Do not use tools.")],
                                      [TextBlock("Reply SECOND only. Do not use tools.")]])
                    self.assertTrue((await client.restore_checkpoint(users[0].message_id))["success"])
                    followup = await self._response(client, "Reply READY only. Do not use tools.")
                    self.assertIn("READY", followup.result.upper())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_stored_archive_keeps_prompt_ids_and_text(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = Path(root) / "workspace"
            cwd.mkdir()
            database = Path(root) / "mirror.sqlite3"
            first_prompt = "Reply FIRST only. Do not use tools."
            second_prompt = "Reply SECOND only. Do not use tools."
            options = KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=[], turn_timeout=90,
                session_store=SQLiteSessionStore(database), session_store_archive=True)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    first = await self._response(client, first_prompt)
                    second = await self._response(client, [{"type": "text", "text": second_prompt}])
                archive_preview = await get_session_messages(
                    session_id, directory=cwd, agent_engine="v3", kiro_history_source="archive-text")
                preview_users = {item.message_id: item for item in archive_preview if isinstance(item, UserMessage)}
                self.assertEqual(preview_users[first.user_message_id].content, [TextBlock(first_prompt)])
                self.assertEqual(preview_users[second.user_message_id].content, [TextBlock(second_prompt)])
                await delete_session(session_id, cwd=cwd)
                saved_id = session_id
                session_id = None
                messages = await get_session_archive_messages_from_store(
                    SQLiteSessionStore(database), saved_id, directory=cwd)
                users = {item.message_id: item for item in messages if isinstance(item, UserMessage)}
                self.assertTrue(first.user_message_id)
                self.assertTrue(second.user_message_id)
                self.assertNotEqual(first.user_message_id, second.user_message_id)
                self.assertEqual(users[first.user_message_id].content, [TextBlock(first_prompt)])
                self.assertEqual(users[second.user_message_id].content, [TextBlock(second_prompt)])
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_archive_tool_messages_match_reported_call_and_result(self):
        seen = []
        @tool("probe", "Return a synthetic marker", {"value": str})
        async def probe(args):
            seen.append(args["value"])
            return {"content": [{"type": "text", "text": "TOOL_RESULT_" + args["value"]}]}

        async def gate(event, tool_id, context):
            name, data = event.get("tool_name", ""), event.get("tool_input") or {}
            allowed = ((name == "tool_load" and data.get("tool_ids") == ["probe_server::probe"]) or
                       name == "mcp_probe_server_probe")
            return {} if allowed else {"block": "Only the synthetic test tool is allowed"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "workspace"
            cwd.mkdir()
            env = {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}
            store = SQLiteSessionStore(root / "mirror.sqlite3")
            options = KiroAgentOptions(cwd=cwd, env=env, agent_engine="v3", tools=["@mcp"], turn_timeout=90,
                permission_mode="trustAll", native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[gate])]},
                mcp_servers={"probe_server": create_mcp_server("probe_server", tools=[probe])},
                session_store=store, session_store_archive=True)
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    result = await self._response(client, "Call probe_server::probe with value SYNTHETIC_VALUE. "
                                                         "Report its result and use no other tools.")
                    self.assertEqual(seen, ["SYNTHETIC_VALUE"])
                    self.assertIn("TOOL_RESULT_SYNTHETIC_VALUE", result.result.upper())
                records = await get_session_archive_history(session_id, directory=cwd, env=env)
                call = next(record for record in records if record["payload"].get("type") == "tool_call"
                            and record["payload"].get("title") == "@probe_server/probe")
                tool_id = call["payload"]["toolCallId"]
                tool_result = next(record for record in records if record["payload"].get("type") == "tool_result"
                                   and record["payload"].get("toolCallId") == tool_id)
                messages = await get_session_messages(session_id, directory=cwd, env=env, agent_engine="v3",
                                                      kiro_history_source="archive-tools")
                matched = next(m for m in messages if isinstance(m, ToolCall) and m.tool_call_id == tool_id)
                self.assertEqual((matched.title, matched.raw_input, matched.status, matched.raw_output),
                                 ("@probe_server/probe", call["payload"]["args"], "completed",
                                  tool_result["payload"]["content"]))
                self.assertEqual(matched.kiro_metadata, {"toolName": call["payload"]["toolName"]})
                stored = await get_session_archive_messages_from_store(
                    SQLiteSessionStore(root / "mirror.sqlite3"), session_id, directory=cwd,
                    include_tool_messages=True)
                self.assertIn(matched, stored)
                replayed = await get_session_messages(session_id, directory=cwd, env=env, agent_engine="v3",
                                                       include_observed_tool_messages=True)
                replay_call = next(m for m in replayed if isinstance(m, ToolCall) and m.tool_call_id == tool_id)
                self.assertEqual((replay_call.title, replay_call.status), ("@probe_server/probe", "completed"))
                self.assertIn("SYNTHETIC_VALUE", str(replay_call.raw_input))
                self.assertIn("TOOL_RESULT_SYNTHETIC_VALUE", str((replay_call.raw_output, replay_call.content)))
                mirrored = await get_session_messages_from_store(
                    SQLiteSessionStore(root / "mirror.sqlite3"), session_id, directory=cwd,
                    include_observed_tool_messages=True)
                mirror_call = next((m for m in mirrored if isinstance(m, ToolCall) and m.tool_call_id == tool_id), None)
                if mirror_call is None or mirror_call.status != "completed":
                    entries = await store.load({"project_key": project_key_for_directory(str(cwd)),
                                                "session_id": session_id})
                    summary = []
                    for entry in entries:
                        data = entry.get("data")
                        event = data if entry.get("type") == "update" and isinstance(data, dict) else {}
                        raw = event.get("rawOutput")
                        summary.append((entry.get("type"), event.get("sessionUpdate"),
                                        event.get("toolCallId") == tool_id, event.get("status"),
                                        sorted(raw) if isinstance(raw, dict) else type(raw).__name__))
                    self.fail(f"Mirrored tool call was not reported; event summary: {summary!r}")
                self.assertEqual(mirror_call.title, "@probe_server/probe")
                self.assertIn("TOOL_RESULT_SYNTHETIC_VALUE", str((mirror_call.raw_output, mirror_call.content)))
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd, env=env)

    async def test_v3_continuous_receiver_accepts_a_followup_from_result(self):
        import asyncio
        with tempfile.TemporaryDirectory() as cwd:
            options = KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=[], turn_timeout=90,
                                       receive_messages_mode="continuous")
            session_id = None
            task = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    results = []
                    async def listen():
                        listener = client.receive_messages()
                        try:
                            async for message in listener:
                                if isinstance(message, ResultMessage):
                                    results.append(message.result)
                                    if len(results) == 1:
                                        await client.query("Reply with SECOND only. Do not use tools.")
                                    else:
                                        break
                        finally:
                            await listener.aclose()
                    task = asyncio.create_task(listen())
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    await client.query("Reply with FIRST only. Do not use tools.")
                    await asyncio.wait_for(task, 180)
                    self.assertEqual(len(results), 2)
                    self.assertIn("FIRST", results[0].upper())
                    self.assertIn("SECOND", results[1].upper())
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_buffered_stream_input(self):
        seen = []
        async def source():
            for token in ("FIRST", "SECOND"):
                seen.append(token)
                yield f"Reply with {token} only. Do not use tools."
        with tempfile.TemporaryDirectory() as cwd:
            options = KiroAgentOptions(cwd=cwd, agent_engine="v3", tools=[], turn_timeout=90,
                                       stream_input_mode="buffered")
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query(source())
                    self.assertEqual(seen, ["FIRST", "SECOND"])
                    self.assertIsNotNone(client._turn)
                    results = [m.result async for m in client.receive_messages() if isinstance(m, ResultMessage)]
                    self.assertEqual(len(results), 2)
                    self.assertIn("FIRST", results[0].upper())
                    self.assertIn("SECOND", results[1].upper())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_checkpoint_restoration(self):
        with tempfile.TemporaryDirectory() as cwd:
            file = Path(cwd) / 'demo.txt'
            file.write_text('ORIGINAL\n')
            options = KiroAgentOptions(cwd=cwd, agent_engine='v3', permission_mode='trustAll')
            session_id = None
            try:
                async with KiroSDKClient(options) as client:
                    session_id = client.session_id
                    await client.query('Use the file editing tool to replace demo.txt contents with exactly '
                                       'CHANGED and a newline. Do not use shell or modify other files.')
                    messages = [m async for m in client.receive_response()]
                    ids = [m.data.get('_meta', {}).get('kiro', {}).get('userMessageId')
                           for m in messages if isinstance(m, KiroEvent)
                           and m.data.get('_meta', {}).get('kiro', {}).get('userMessageId')]
                    self.assertEqual(file.read_text(), 'CHANGED\n')
                    self.assertEqual(len(ids), 1)
                    result = await client.restore_checkpoint(ids[0])
                    self.assertTrue(result['success'])
                    self.assertEqual(file.read_text(), 'ORIGINAL\n')
                    result = await self._response(client, 'Reply with OK only; do not use tools.')
                    self.assertEqual(result.result.strip().upper(), 'OK')
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_context_files(self):
        with tempfile.TemporaryDirectory() as cwd:
            file = Path(cwd) / 'context.txt'
            file.write_text('Example context\n')
            session_id = None
            try:
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine='v3')) as client:
                    session_id = client.session_id
                    self.assertTrue((await client.add_kiro_context('context.txt'))['success'])
                    self.assertEqual(len((await client.list_kiro_context())['entries']), 1)
                    self.assertTrue((await client.remove_kiro_context('context.txt'))['removed'])
                    self.assertEqual((await client.list_kiro_context())['entries'], [])
                    self.assertTrue(file.exists())
            finally:
                if session_id:
                    await delete_session(session_id, cwd=cwd)

    async def test_v3_rewind_files_with_conversation_fork(self):
        with tempfile.TemporaryDirectory() as cwd:
            file = Path(cwd) / 'demo.txt'
            file.write_text('ORIGINAL\n')
            options = KiroAgentOptions(cwd=cwd, agent_engine='v3', permission_mode='trustAll')
            parent_id = fork_id = None
            try:
                async with KiroSDKClient(options) as client:
                    parent_id = client.session_id
                    await client.query('Use the file editing tool to replace demo.txt contents with exactly '
                                       'CHANGED and a newline. Do not use shell or modify other files.')
                    messages = [m async for m in client.receive_response()]
                    ids = [m.data.get('_meta', {}).get('kiro', {}).get('userMessageId')
                           for m in messages if isinstance(m, KiroEvent)
                           and m.data.get('_meta', {}).get('kiro', {}).get('userMessageId')]
                    self.assertEqual(len(ids), 1)
                    self.assertEqual(file.read_text(), 'CHANGED\n')
                    await self._response(client, 'Remember the marker ZEBRA473. Reply OK; do not use tools.')
                    outcome = await client.rewind_files_preserving_conversation(ids[0])
                    fork_id = outcome['fork_session_id']
                    self.assertNotEqual(fork_id, parent_id)
                    self.assertEqual(file.read_text(), 'ORIGINAL\n')
                async with KiroSDKClient(KiroAgentOptions(cwd=cwd, agent_engine='v3', resume=fork_id)) as fork:
                    prompts = [e.get('content', {}).get('text', '') for e in fork.history
                               if e.get('sessionUpdate') == 'user_message_chunk']
                    self.assertTrue(any('ZEBRA473' in prompt for prompt in prompts))
                    result = await self._response(fork, 'Reply with the marker I asked you to remember, and nothing else.')
                    self.assertIn('ZEBRA473', result.result.upper())
            finally:
                if fork_id:
                    await delete_session(fork_id, cwd=cwd)
                if parent_id:
                    await delete_session(parent_id, cwd=cwd)

    async def test_v3_restore_session_from_mirror_archive(self):
        with tempfile.TemporaryDirectory() as root:
            origin = Path(root) / 'origin'
            target = Path(root) / 'target'
            origin.mkdir()
            target.mkdir()
            store = InMemorySessionStore()
            try:
                options = KiroAgentOptions(cwd=origin, agent_engine='v3', session_store=store,
                                           session_store_archive=True, max_reported_credits=0.000001)
                async with KiroSDKClient(options) as client:
                    original_id = client.session_id
                    await self._response(client, 'Remember the marker JASMINE482. Reply OK; do not use tools.')
                    self.assertGreater(client.reported_credits, 0.000001)
                    with self.assertRaises(ReportedCreditLimitError):
                        await client.query('This turn must not be sent.')
                await delete_session(original_id, cwd=origin)
                restored_options = KiroAgentOptions(
                    cwd=target, agent_engine='v3', resume=original_id, session_store=store,
                    resume_from_store=True, session_store_source_directory=origin)
                async with KiroSDKClient(restored_options) as fork:
                    restored_id = fork.restored_session_id
                    self.assertEqual(fork.session_id, restored_id)
                    self.assertNotEqual(restored_id, original_id)
                    prompts = [event.get('content', {}).get('text', '') for event in fork.history
                               if event.get('sessionUpdate') == 'user_message_chunk']
                    self.assertTrue(any('JASMINE482' in text for text in prompts))
                    result = await self._response(fork, 'Reply with the marker I asked you to remember, and nothing else.')
                    self.assertIn('JASMINE482', result.result.upper())
            finally:
                for cwd in (origin, target):
                    for session in await list_sessions(cwd=cwd):
                        if session.get('sessionId'):
                            await delete_session(session['sessionId'], cwd=cwd)

    async def test_v3_import_existing_session_to_persistent_store(self):
        with tempfile.TemporaryDirectory() as root:
            origin, target = Path(root) / 'origin', Path(root) / 'target'
            origin.mkdir()
            target.mkdir()
            database = Path(root) / 'mirror.sqlite3'
            try:
                async with KiroSDKClient(KiroAgentOptions(cwd=origin, agent_engine='v3')) as client:
                    original_id = client.session_id
                    await self._response(client, 'Remember the marker DAISY384. Reply OK; do not use tools.')
                await import_session_to_store(original_id, SQLiteSessionStore(database), directory=origin, batch_size=2)
                reopened = SQLiteSessionStore(database)
                listed = await list_sessions_from_store(reopened, directory=origin)
                self.assertEqual(listed[0].session_id, original_id)
                self.assertTrue(listed[0].title)
                self.assertIsInstance(listed[0].created_at, int)
                messages = await get_session_messages_from_store(reopened, original_id, directory=origin)
                self.assertTrue(any('DAISY384' in str(message.content) for message in messages))
                await delete_session(original_id, cwd=origin)
                options = KiroAgentOptions(cwd=target, agent_engine='v3', resume=original_id,
                                           session_store=reopened, resume_from_store=True,
                                           session_store_source_directory=origin)
                async with KiroSDKClient(options) as restored:
                    result = await self._response(restored, 'Reply with the marker I asked you to remember, and nothing else.')
                    self.assertIn('DAISY384', result.result.upper())
            finally:
                for cwd in (origin, target):
                    for session in await list_sessions(cwd=cwd):
                        if session.get('sessionId'):
                            await delete_session(session['sessionId'], cwd=cwd)
