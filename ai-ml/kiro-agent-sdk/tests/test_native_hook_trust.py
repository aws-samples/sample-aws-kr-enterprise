"""L11: native command hooks must not open an implicit path into host callbacks."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient
from kiro_agent_sdk.hooks import NATIVE_HOOK_TRIGGERS

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


@unittest.skipUnless(os.name == "posix", "native hook integration requires POSIX")
class NativeHookTrustTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seen = []

    def options(self, **changes):
        return KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3",
            env={"KIRO_HOME": str(self.root / "kiro")}, **changes)

    async def hook(self, event, tool_id, context):
        self.seen.append(event)
        return {}

    async def assert_rejected_without_starting(self, options):
        client = KiroSDKClient(options)
        with patch("kiro_agent_sdk.hooks.NativeHookBridge.start", new_callable=AsyncMock) as broker, \
                patch("kiro_agent_sdk.client._spawn_cli", new_callable=AsyncMock) as cli:
            try:
                with self.assertRaisesRegex(ValueError, "Native hook bridge disabled"):
                    await client.connect()
            finally:
                await client.disconnect()
            broker.assert_not_awaited()
            cli.assert_not_awaited()
        self.assertEqual(self.seen, [])
        self.assertIsNone(client._native_hook_bridge)
        self.assertFalse(list((self.root / "kiro").rglob("*.json")))

    async def test_default_rejects_all_direct_native_events_before_opening_broker(self):
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
                      "PostFileCreate", "PostFileSave", "PostFileDelete"):
            with self.subTest(event=event):
                await self.assert_rejected_without_starting(self.options(
                    native_hooks={{v: k for k, v in NATIVE_HOOK_TRIGGERS.items()}[event]: [HookMatcher(hooks=[self.hook])]}))
        await self.assert_rejected_without_starting(self.options(
            native_hooks={"preToolUse": [HookMatcher(hooks=[self.hook])]}))

    async def test_same_user_is_explicit_compatibility_not_origin_authentication(self):
        # Deliberately submit an event ourselves, without Kiro launching a hook.
        # Keep this limitation visible instead of claiming the opt-in mode isolates code.
        options = self.options(native_hook_bridge_mode="same-user",
            native_hooks={"postToolUse": [HookMatcher(hooks=[self.hook])]})
        async with KiroSDKClient(options) as client:
            path = client._native_hook_bridge._credential_path
            credentials = json.loads(path.read_bytes())
            reader, writer = await asyncio.open_connection("127.0.0.1", credentials["port"])
            try:
                writer.write((credentials["token"] + "\n" + json.dumps({
                    "hook_event_name": "PostToolUse", "tool_name": "synthetic-spoof"}) + "\n").encode())
                await writer.drain()
                result = json.loads(await asyncio.wait_for(reader.readline(), 5))
            finally:
                writer.close()
                await writer.wait_closed()
            self.assertEqual(result, {"block": False})
            self.assertEqual(self.seen[0]["tool_name"], "synthetic-spoof")
        self.assertFalse(path.exists())

    async def test_trust_mode_is_validated_and_cannot_be_changed_in_place(self):
        for value in (None, True, 1, "", "trusted", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "native_hook_bridge_mode"):
                KiroSDKClient(self.options(native_hook_bridge_mode=value))
        options = self.options(native_hooks={"preToolUse": [HookMatcher(hooks=[self.hook])]})
        client = KiroSDKClient(options)
        options.native_hook_bridge_mode = "same-user"
        with patch("kiro_agent_sdk.hooks.NativeHookBridge.start", new_callable=AsyncMock) as broker:
            with self.assertRaisesRegex(ValueError, "Create a new client"):
                await client.connect()
            broker.assert_not_awaited()

from test_agentcore import HAS_STARLETTE, HEADER, request


@unittest.skipUnless(HAS_STARLETTE, "requires AgentCore dependencies")
class AgentCoreNativeHookTrustTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_factory_cannot_implicitly_enable_native_bridge(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        hook = AsyncMock(return_value={})
        with tempfile.TemporaryDirectory() as directory:
            app = create_agentcore_app(
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), cli_path=FAKE, api_key="synthetic"),
                options_factory=lambda *_: KiroAgentOptions(
                    native_hooks={"postToolUse": [HookMatcher(hooks=[hook])]}))
            with patch("kiro_agent_sdk.hooks.NativeHookBridge.start", new_callable=AsyncMock) as broker, \
                    patch("kiro_agent_sdk.client._spawn_cli", new_callable=AsyncMock) as cli:
                try:
                    status, body, _ = await request(app, "POST", "/invocations",
                        b'{"prompt":"hello"}', [(HEADER, b"native-hook-trust-session")])
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body), {"error": "Agent invocation failed", "is_error": True})
                    broker.assert_not_awaited()
                    cli.assert_not_awaited()
                    hook.assert_not_awaited()
                finally:
                    await app.state.kiro_agentcore.close()

