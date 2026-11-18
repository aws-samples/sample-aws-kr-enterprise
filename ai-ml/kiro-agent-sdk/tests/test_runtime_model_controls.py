"""Regression specifications; live deployment remains separately opt-in."""
import os
import asyncio
import shutil
import subprocess
import unittest
from pathlib import Path

from kiro_agent_sdk import AgentDefinition, HookMatcher, KiroSDKClient
from kiro_agent_sdk.hooks import NativeHookBridge
from kiro_agent_sdk.native_hook_runtime import command_result
from kiro_agent_sdk.runtime_model import JAVASCRIPT as MODEL
from kiro_agent_sdk.runtime_permission import JAVASCRIPT as PERMISSION
from kiro_agent_sdk.runtime_policy import JAVASCRIPT as POLICY
from kiro_agent_sdk.runtime_session import JAVASCRIPT as SESSION
from kiro_agent_sdk.runtime_posttool import JAVASCRIPT as POST
import test_runtime_policies as policy_fixtures


@unittest.skipUnless(os.name == "posix", "requires POSIX runtime")
class RuntimeModelClientTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = policy_fixtures.RuntimePolicyTests.asyncSetUp
    options = policy_fixtures.RuntimePolicyTests.options
    prompts = policy_fixtures.RuntimePolicyTests.prompts

    async def test_runtime_fallback_is_per_prompt_and_can_resume(self):
        options = self.options(fallback_model="model-two", fallback_model_mode="runtime", resume="old")
        async with KiroSDKClient(options) as client:
            for prompt in ("first", "second"):
                await client.query(prompt)
                _ = [message async for message in client.receive_response()]
        policies = [p["_meta"]["kiro"]["sdkRuntime"] for p in self.prompts()]
        self.assertEqual([p["fallbackModel"] for p in policies], ["model-two", "model-two"])
        self.assertNotEqual(policies[0]["requestId"], policies[1]["requestId"])

    async def test_enabled_thinking_is_sent_to_kiro(self):
        async with KiroSDKClient(self.options(thinking=True)) as client:
            self.assertTrue(client.session_info["received"]["_meta"]["kiro"]["thinkingEnabled"])

    async def test_selected_agent_turn_limit_intersects_main_limit(self):
        options = self.options(agents={"worker": AgentDefinition("Work", "Work", max_turns=2)},
                               agent="worker", tools=None, max_turns=4)
        options.env["FAKE_RUNTIME_MODEL_TURNS"] = "3"
        async with KiroSDKClient(options) as client:
            await client.query("hello")
            messages = [message async for message in client.receive_response()]
            self.assertEqual((messages[-1].stop_reason, messages[-1].model_turns), ("max_turn_requests", 2))
        policy = self.prompts()[-1]["_meta"]["kiro"]["sdkRuntime"]
        self.assertEqual(policy["maxTurns"], 2)
        self.assertEqual(policy["agentMaxTurns"], {"worker": 2})

    async def test_fallback_configuration_changes_are_rejected_before_send(self):
        options = self.options(fallback_model_mode="runtime", fallback_model="model-two")
        async with KiroSDKClient(options) as client:
            options.fallback_model = "other"
            with self.assertRaisesRegex(ValueError, "new client"):
                await client.query("blocked")

    async def test_cancel_resistant_native_warning_blocks_reconnect_until_cleaned(self):
        release = asyncio.Event()
        async def notice(_event):
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
        client = KiroSDKClient(self.options(on_native_hook_system_message=notice))
        try:
            await client.connect()
            with self.assertRaises(Exception):
                await client._deliver_native_hook_message("SubagentStop",
                    {"session_id": client.session_id, "agent_id": "child"}, "notice", timeout=.02)
            await asyncio.sleep(.01)
            self.assertTrue(client._hook_failure_runner.tasks)
            with self.assertRaisesRegex(Exception, "cleaning up|cleanup"):
                await client.disconnect()
            with self.assertRaises(Exception):
                await client.connect()
        finally:
            release.set()
            await asyncio.sleep(.02)
            await client.disconnect()


class RuntimeModelJavascriptTests(unittest.TestCase):

    def run_javascript(self, script):
        node = str(Path.home() / ".local/share/kiro-cli/node")
        if not Path(node).is_file():
            node = shutil.which("node")
        if not node:
            self.skipTest("requires Node")
        result = subprocess.run([node, "--input-type=module", "-e",
            POLICY + SESSION + MODEL + PERMISSION + POST + '\nimport assert from "node:assert/strict";\n' + script],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class NativeRuntimeInputEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_replacements_are_seen_by_next_hook_and_kept_out_of_command_stdout(self):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "read", "tool_input": {"path": "old"}}
        seen = []
        async def first(event, *_):
            seen.append(event["tool_input"])
            return {'tool_input': {"path": "new"}}
        async def second(event, *_):
            seen.append(event["tool_input"])
            return {'ask': "review", 'context': "review context"}
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[first, second])]}, supports_ask=True)
        try:
            response = await bridge.dispatch(payload, input_writable=True)
            self.assertEqual(seen, [{"path": "old"}, {"path": "new"}])
            self.assertEqual(payload["tool_input"], {"path": "old"})
            result = command_result(payload, response)
            self.assertEqual(result["sdkUpdatedInput"], {"path": "new"})
            self.assertEqual(result["sdkAdditionalContext"], "review context")
            self.assertNotIn("new", result["stdout"])
            self.assertIn("ask", result["stdout"])
        finally:
            await bridge.close()

    async def test_denial_discards_prior_edits_and_default_bridge_rejects_edits(self):
        async def edit(*_):
            return {'tool_input': {"path": "new"}}
        async def deny(*_):
            return {'block': True}
        payload = {"hook_event_name": "PreToolUse", "tool_name": "read", "tool_input": {}}
        for hooks, writable in (([edit], False), ([edit, deny], True)):
            bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=hooks)]})
            try:
                response = await bridge.dispatch(payload, input_writable=writable)
                self.assertTrue(response["block"])
                self.assertNotIn("sdkUpdatedInput", command_result(payload, response))
            finally:
                await bridge.close()

    async def test_invalid_edits_fail_closed(self):
        for value in ([], {"number": float("nan")}, {"nested": (1, 2)}, {1: "value"},
                      {"text": "x" * 1048577}):
            async def edit(*_, value=value):
                return {'tool_input': value}
            bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[edit])]})
            try:
                response = await bridge.dispatch({"hook_event_name": "PreToolUse", "tool_name": "read"},
                                                 input_writable=True)
                self.assertTrue(response["block"])
            finally:
                await bridge.close()

    async def test_allow_is_explicit_and_later_edits_or_ask_prevent_grant_reuse(self):
        async def allow(*_):
            return {'allow': True, 'tool_input': {"path": "approved"}}
        async def change(*_):
            return {'tool_input': {"path": "other"}}
        async def ask(*_):
            return {'ask': True}
        payload = {"hook_event_name": "PreToolUse", "tool_name": "read", "tool_input": {}}
        for hooks, writable, expected in (([allow], True, True), ([allow, change], True, False),
                                          ([allow, ask], True, False), ([allow], False, False)):
            bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=hooks)]}, supports_ask=True)
            try:
                response = await bridge.dispatch(payload, input_writable=writable, allow_once_permitted=True)
                self.assertEqual(response.get("allowOnce", False), expected)
                self.assertEqual(command_result(payload, response).get("sdkAllowOnce", False), expected)
            finally:
                await bridge.close()

    async def test_native_allow_cannot_bypass_independent_sdk_policy(self):
        async def allow(*_):
            return {'allow': True}
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[allow])]})
        try:
            response = await bridge.dispatch({"hook_event_name": "PreToolUse", "tool_name": "shell"},
                                             input_writable=True, allow_once_permitted=False)
            self.assertTrue(response["block"])
            self.assertNotIn("allowOnce", response)
        finally:
            await bridge.close()
