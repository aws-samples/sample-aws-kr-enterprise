"""Native runtime adapter: real ACP fixture, dispatcher and Node failure paths."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError
from kiro_agent_sdk.native_hook_runtime import NativeHookRuntime, RUNNER, command_result, patch_runtime
from kiro_agent_sdk.hooks import NATIVE_HOOK_TRIGGERS

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


@unittest.skipUnless(os.name == "posix", "native hooks require POSIX")
class NativeHookAcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "node_modules/@kiro/agent/dist/server/acp-server.js"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"fixture")
        self.source.with_name("xhr-sync-worker.js").write_bytes(b"worker")
        self.patcher = patch("kiro_agent_sdk.native_hook_runtime.patch_runtime", return_value=b"patched fixture")
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.seen = []

    async def hook(self, data, *_):
        self.seen.append(data)
        return {}

    def options(self, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3",
            native_hook_bridge_mode="runtime-acp", native_hook_runtime_path=self.source,
            native_hooks={"preToolUse": [HookMatcher(hooks=[self.hook], timeout=1)]},
            env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_NATIVE_ACP": "1"})
        values.update(changes)
        return KiroAgentOptions(**values)

    async def invoke(self, client, event="PreToolUse", override=None):
        return await client._request("_kiro/test/native-hook", {
            "sessionId": client.session_id, "event": event, "override": override or {}})

    async def rejected(self, client, **kwargs):
        try:
            reply = await self.invoke(client, **kwargs)
        except (ProtocolError, ConnectionError):
            pass
        except Exception as error:
            from kiro_agent_sdk import CLIConnectionError
            if not isinstance(error, CLIConnectionError):
                raise
        else:
            # The fixture may echo the explicit denial before the harness exits.
            self.assertEqual(reply["error"]["code"], -32800)
            self.assertNotIn("result", reply)
        await asyncio.wait_for(client._process.wait(), 3)
        self.assertIsNotNone(client._reader_error)

    async def test_native_callbacks_use_acp_without_listener_or_credentials(self):
        with patch("kiro_agent_sdk.hooks.NativeHookBridge.start", new_callable=AsyncMock) as listener:
            async with KiroSDKClient(self.options()) as client:
                runtime = client._native_hook_runtime
                directory = runtime.directory
                self.assertIsNone(client._native_hook_bridge.server)
                self.assertIsNone(client._native_hook_bridge._credential_path)
                reply = await self.invoke(client)
                self.assertEqual(reply["result"]["exitCode"], 0)
                self.assertEqual(self.seen[0]["tool_input"], {"command": "probe"})
                listener.assert_not_awaited()
            self.assertFalse(directory.exists())
        self.assertEqual(self.source.read_bytes(), b"fixture")

    async def test_all_registered_native_events_keep_return_contract(self):
        events = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
                  "PostFileCreate", "PostFileSave", "PostFileDelete")
        async def hook(data, *_):
            event = data["hook_event_name"]
            if event == "PreToolUse":
                return {'block': "policy"}
            if event == "Stop":
                return {'continue': "policy"}
            if event in ("SessionStart", "UserPromptSubmit"):
                return {'context': "context"}
            return {}
        triggers = {event: trigger for trigger, event in NATIVE_HOOK_TRIGGERS.items()}
        options = self.options(native_hooks={triggers[event]: [HookMatcher(hooks=[hook])] for event in events})
        async with KiroSDKClient(options) as client:
            for event in events:
                with self.subTest(event=event):
                    result = (await self.invoke(client, event))["result"]
                    self.assertEqual(result["exitCode"], 2 if event == "PreToolUse" else 0)
                    if event == "Stop":
                        self.assertEqual(json.loads(result["stdout"]), {"decision": "block", "reason": "policy"})

    async def test_unregistered_or_wrong_session_frames_abort_without_callbacks(self):
        for change in ({"hookId": "other#hook-0"}, {"command": "kiro-sdk-acp-hook:other"},
                       {"sessionId": "other"}, {"protocol": True}, {"timeoutMs": 0},
                       {"input": {"session_id": "test-session", "hook_event_name": "Stop"}}):
            with self.subTest(change=change):
                client = KiroSDKClient(self.options())
                try:
                    await client.connect()
                    await self.rejected(client, override=change)
                finally:
                    await client.disconnect()
                self.assertEqual(self.seen, [])

    async def test_profile_modification_cannot_invoke_callback(self):
        async with KiroSDKClient(self.options()) as client:
            client._agent_file.write_bytes(client._agent_file.read_bytes() + b" ")
            await self.rejected(client)
            self.assertEqual(self.seen, [])

    async def test_authorization_rechecked_between_callbacks(self):
        allowed = True
        async def first(*_):
            nonlocal allowed
            self.seen.append("first")
            allowed = False
            return {}
        async def authorize():
            return allowed
        options = self.options(native_hooks={"preToolUse": [HookMatcher(hooks=[first, self.hook])]})
        async with KiroSDKClient(options) as client:
            client._interaction_guard = authorize
            await self.rejected(client)
            self.assertEqual(self.seen, ["first"])

    async def test_timeout_does_not_wait_for_callback_that_ignores_cancellation(self):
        release = asyncio.Event()
        entered = asyncio.Event()
        async def stuck(*_):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return {}
        client = KiroSDKClient(self.options(native_hooks={"preToolUse": [HookMatcher(hooks=[stuck])]}))
        try:
            await client.connect()
            await asyncio.wait_for(self.rejected(client, override={"timeoutMs": 400}), 3)
            self.assertTrue(entered.is_set())
            self.assertTrue(client._native_acp_dispatch_tasks)
            with self.assertRaisesRegex(Exception, "previous native ACP hook"):
                await client.connect()
        finally:
            release.set()
            await asyncio.sleep(.05)
            await client.disconnect()

    async def test_runtime_handshake_is_required_and_failed_start_cleans_copy(self):
        options = self.options(env={"KIRO_HOME": str(self.root / "kiro")})
        copies = []
        original = NativeHookRuntime.prepare
        def prepare(runtime, *args, **kwargs):
            original(runtime, *args, **kwargs)
            copies.append(runtime.directory)
        with patch.object(NativeHookRuntime, "prepare", prepare):
            with self.assertRaisesRegex(ProtocolError, "did not confirm"):
                await KiroSDKClient(options).connect()
        self.assertTrue(copies)
        self.assertFalse(copies[0].exists())
        self.assertEqual(self.seen, [])

    async def test_reconnect_uses_fresh_marker_and_binding(self):
        client = KiroSDKClient(self.options())
        async with client:
            first = client._native_hook_runtime.command
            await self.invoke(client)
        async with client:
            self.assertNotEqual(first, client._native_hook_runtime.command)
            await self.invoke(client)
        self.assertEqual(len(self.seen), 2)

    async def test_sdk_pretool_adapter_uses_the_same_dispatch_contract(self):
        from kiro_agent_sdk import PermissionResultAllow
        async def hook(data, *_):
            self.seen.append(data)
            return {'block': "policy"}
        options = self.options(native_hooks={}, sdk_pretool_hook_mode="native",
            hooks={"preToolUse": [HookMatcher(hooks=[hook])]},
            can_use_tool=AsyncMock(return_value=PermissionResultAllow()))
        options.env["FAKE_NATIVE_ACP_PROMPT"] = "1"
        async with KiroSDKClient(options) as client:
            await client.query("hello")
            messages = [message async for message in client.receive_response()]
            self.assertTrue(messages)
            self.assertEqual(self.seen[0]["hook_event_name"], "PreToolUse")
            options.can_use_tool.assert_not_awaited()
            self.assertIsNone(client._reader_error)

    async def test_explicit_cancellation_denies_hook_without_poisoning_session(self):
        release = asyncio.Event()
        async def hook(data, *_):
            await release.wait()
            self.seen.append(data)
            return {}
        options = self.options(native_hooks={"preToolUse": [HookMatcher(hooks=[hook])]})
        async with KiroSDKClient(options) as client:
            reply = await client._request("_kiro/test/native-hook", {
                "sessionId": client.session_id, "event": "PreToolUse", "cancel": True})
            self.assertEqual(reply["error"]["code"], -32800)
            self.assertEqual(self.seen, [])
            self.assertIsNone(client._reader_error)
            release.set()
            await self.invoke(client)
            self.assertEqual(len(self.seen), 1)

    async def test_unknown_bundle_or_cli_version_rejected_before_acp_start(self):
        self.patcher.stop()
        with patch("kiro_agent_sdk.client._spawn_cli", new_callable=AsyncMock) as spawn:
            client = KiroSDKClient(self.options())
            with self.assertRaisesRegex(ValueError, "Untested Kiro"):
                await client.connect()
            self.assertIsNone(client._native_hook_bridge)
            spawn.assert_not_awaited()
        self.patcher.start()
        options = self.options(env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_VERSION": "kiro-cli 9.0.0"})
        with self.assertRaises(Exception):
            await KiroSDKClient(options).connect()


class NativeHookRuntimeTests(unittest.TestCase):
    def test_unknown_source_cannot_be_patched(self):
        with self.assertRaisesRegex(ValueError, "Untested"):
            patch_runtime(b"arbitrary JavaScript")

    def test_stop_failure_finishes_and_pretool_failure_denies(self):
        from kiro_agent_sdk.hooks import NativeHookBridge
        for event, code in (("Stop", 0), ("PreToolUse", 2)):
            payload = {"hook_event_name": event}
            result = command_result(payload, NativeHookBridge.failure_response(payload))
            self.assertEqual(result["exitCode"], code)
            self.assertEqual(result["stdout"], "")

    def test_javascript_runner_preserves_denial_on_failure_timeout_and_cancel(self):
        node = str(Path.home() / ".local/share/kiro-cli/node")
        if not Path(node).is_file():
            node = shutil.which("node")
        if not node:
            self.skipTest("requires Node")
        expression = RUNNER.removeprefix("processRunner:").removesuffix(",clock:new eje")
        from kiro_agent_sdk.runtime_permission import JAVASCRIPT as PERMISSION
        from kiro_agent_sdk.runtime_posttool import JAVASCRIPT as POSTTOOL
        script = 'const {AsyncLocalStorage:KiroSdkPolicyStorage}=require("node:async_hooks");\n' + PERMISSION + POSTTOOL + r'''
const assert=require("node:assert/strict");
const p={};let fallback=0, handler=async()=>{throw Error("private failure")};
class tje{spawn(){fallback++;return Promise.resolve({exitCode:0})}}
async function fm(c,method,params){assert.equal(method,"_kiro/sdk/hooks/callback");return handler(params)}
const runner=(function(){return EXPRESSION}).call({connection:{}});
function request(event="PreToolUse"){return {command:"kiro-sdk-acp-hook:test",sdkHookId:"owned#hook-0",
  stdin:JSON.stringify({session_id:"session",hook_event_name:event}),signal:new AbortController().signal,timeoutMs:15}}
(async()=>{
  assert.equal((await runner.spawn(request())).exitCode,2);
  handler=async()=>({exitCode:0,stdout:"ok",stderr:"",timedOut:false,cancelled:false});
  assert.equal((await runner.spawn(request())).stdout,"ok");
  handler=async()=>({exitCode:0});assert.equal((await runner.spawn(request())).exitCode,2);
  handler=async()=>new Promise(resolve=>setTimeout(()=>resolve({exitCode:0}),80));
  const late=await runner.spawn(request());assert.equal(late.exitCode,2);assert.equal(late.timedOut,true);
  const stop=await runner.spawn(request("Stop"));assert.equal(stop.exitCode,0);assert.equal(stop.stdout,"");
  const controller=new AbortController();const r=request();r.signal=controller.signal;
  const pending=runner.spawn(r);controller.abort();assert.equal((await pending).exitCode,2);
  await runner.spawn({...request(),command:"ordinary-hook"});assert.equal(fallback,1);
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace("EXPRESSION", expression)
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


from test_agentcore import HAS_STARLETTE, HEADER, request


@unittest.skipUnless(HAS_STARLETTE, "requires AgentCore dependencies")
class AgentCoreNativeAcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_scope_and_revocation_protect_native_callbacks(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "node_modules/@kiro/agent/dist/server/acp-server.js"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"fixture")
            source.with_name("xhr-sync-worker.js").write_bytes(b"worker")
            seen, allowed = [], True
            async def authorize(req, session_id):
                return allowed and req.headers.get("x-owner") == "owner"
            async def hook(data, *_):
                seen.append(data["session_id"])
                return {}
            def options(*_):
                return KiroAgentOptions(native_hook_bridge_mode="runtime-acp",
                    native_hook_runtime_path=source,
                    native_hooks={"preToolUse": [HookMatcher(hooks=[hook])]},
                    env={"FAKE_NATIVE_ACP": "1", "FAKE_NATIVE_ACP_PROMPT": "1"})
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root / "state", cli_path=FAKE, api_key="synthetic")
            app = create_agentcore_app(config, options_factory=options, session_authorizer=authorize)
            with patch("kiro_agent_sdk.native_hook_runtime.patch_runtime", return_value=b"patched fixture"):
                try:
                    headers = [(HEADER, b"native-acp-owned-session")]
                    self.assertEqual((await request(app, "POST", "/invocations", b'{"prompt":"hello"}', headers))[0], 403)
                    status, body, _ = await request(app, "POST", "/invocations", b'{"prompt":"hello"}',
                        [*headers, (b"x-owner", b"owner")])
                    self.assertEqual(status, 200)
                    self.assertFalse(json.loads(body).get("is_error"))
                    self.assertEqual(len(seen), 1)
                    allowed = False
                    self.assertEqual((await request(app, "POST", "/invocations", b'{"prompt":"private"}',
                        [*headers, (b"x-owner", b"owner")]))[0], 403)
                    self.assertEqual(len(seen), 1)
                finally:
                    await app.state.kiro_agentcore.close()

    async def test_runtime_mode_requires_session_authorizer(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        with tempfile.TemporaryDirectory() as directory:
            app = create_agentcore_app(
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), cli_path=FAKE, api_key="synthetic"),
                options_factory=lambda *_: KiroAgentOptions(native_hook_bridge_mode="runtime-acp"))
            try:
                _, body, _ = await request(app, "POST", "/invocations", b'{"prompt":"hello"}',
                    [(HEADER, b"native-acp-no-owner")])
                self.assertTrue(json.loads(body)["is_error"])
            finally:
                await app.state.kiro_agentcore.close()
