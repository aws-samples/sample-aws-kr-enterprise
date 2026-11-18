import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, PermissionResultAllow
from test_transport import FakeTransport


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


async def invoke_native(bridge, payload):
    port = bridge.server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write((bridge.token + "\n" + json.dumps(payload) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 5))
    finally:
        writer.close()
        await writer.wait_closed()


@unittest.skipUnless(os.name == "posix", "native Kiro hooks require POSIX")
class NativeSdkPretoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_hook_is_normalized_and_not_repeated_at_permission(self):
        with tempfile.TemporaryDirectory() as root:
            seen = []

            async def first(event, tool_id, context):
                seen.append((event["hook_event_name"], event["session_id"], tool_id))
                event["tool_input"]["path"] = "mutated"
                return {}

            async def second(event, tool_id, context):
                seen.append(event["tool_input"]["path"])
                return {'block': "protected"}

            async def allow(name, data, context):
                return PermissionResultAllow()

            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                env={"KIRO_HOME": str(Path(root) / "kiro")}, can_use_tool=allow,
                native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native", hooks={"preToolUse": [HookMatcher(hooks=[first, second])]})
            async with KiroSDKClient(options) as client:
                response = await invoke_native(client._native_hook_bridge, {
                    "hook_event_name": "preToolUse", "tool_name": "write",
                    "tool_call_id": "tool-1", "tool_input": {"path": "original"}})
                self.assertEqual(response, {"block": True, "reason": "protected"})
                self.assertEqual(seen, [("PreToolUse", client.session_id, "tool-1"), "original"])
                await client.query("permission")
                self.assertEqual([item async for item in client.receive_response()][-1].result, "yes")
                self.assertEqual(len(seen), 2)

    async def test_native_ask_and_unsupported_allow_fail_closed(self):
        for decision, expected in (("ask", {"block": False, "ask": "review"}),
                                   ("allow", {"block": True, "reason": "SDK hook failed"})):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as root:
                async def hook(event, tool_id, context):
                    return {decision: "review"}
                options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                    env={"KIRO_HOME": str(Path(root) / "kiro")}, native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native",
                    hooks={"preToolUse": [HookMatcher(hooks=[hook])]})
                async with KiroSDKClient(options) as client:
                    response = await invoke_native(client._native_hook_bridge, {
                        "hook_event_name": "PreToolUse", "tool_name": "write", "tool_input": {}})
                    self.assertEqual(response, expected)

    async def test_invalid_native_common_controls_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            output = {}
            async def hook(event, tool_id, context):
                return output
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                env={"KIRO_HOME": str(Path(root) / "kiro")}, native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native",
                hooks={"preToolUse": [HookMatcher(hooks=[hook])]})
            async with KiroSDKClient(options) as client:
                for value in ({"block": 1}, {"unknown": 1}):
                    output.clear()
                    output.update(value)
                    response = await invoke_native(client._native_hook_bridge, {
                        "hook_event_name": "PreToolUse", "tool_name": "write", "tool_input": {}})
                    self.assertEqual(response, {"block": True, "reason": "SDK hook failed"})

    async def test_hook_configuration_changes_require_a_new_client(self):
        async def hook(event, tool_id, context):
            return {}
        with tempfile.TemporaryDirectory() as root:
            matcher = HookMatcher(hooks=[hook])
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                env={"KIRO_HOME": str(Path(root) / "kiro")}, native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native",
                hooks={"preToolUse": [matcher]})
            async with KiroSDKClient(options) as client:
                matcher.matcher = "changed"
                with self.assertRaisesRegex(ValueError, "new client"):
                    await client.query("must not run")
        with self.assertRaisesRegex(ValueError, "local CLI"):
            KiroSDKClient(KiroAgentOptions(agent_engine="v3", native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native"),
                          transport=FakeTransport())
        with self.assertRaisesRegex(ValueError, "Kiro V3"):
            KiroSDKClient(KiroAgentOptions(agent_engine="v2", native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native"))
