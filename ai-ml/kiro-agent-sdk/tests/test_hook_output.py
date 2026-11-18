import asyncio
import unittest

from kiro_agent_sdk import HookMatcher, ProtocolError
from kiro_agent_sdk.hook_output import HOOK_OUTPUT_KEYS, parse_hook_output
from kiro_agent_sdk.hooks import NATIVE_HOOK_TRIGGERS, NativeHookBridge
from kiro_agent_sdk.native_hook_runtime import command_result


class HookOutputTests(unittest.TestCase):
    def test_reasons_flags_and_empty_values(self):
        self.assertEqual(parse_hook_output("PreToolUse", {"block": "policy"}, {"block"}), {"block": "policy"})
        self.assertEqual(parse_hook_output("PreToolUse", {"ask": True}, {"ask"}), {"ask": None})
        self.assertEqual(parse_hook_output("Stop", {"continue": False, "warning": None}, {"continue", "warning"}), {})
        self.assertEqual(parse_hook_output("UserPromptSubmit", {"context": "  ", "title": " T "},
                                           {"context", "title"}), {"title": "T"})
        self.assertEqual(parse_hook_output("PreToolUse", {"allow": True, "tool_input": {"command": "ls"}},
                                           {"allow", "tool_input"}), {"allow": True, "tool_input": {"command": "ls"}})

    def test_invalid_outputs_are_rejected(self):
        for value, allowed in (([], HOOK_OUTPUT_KEYS), ({"decision": "block"}, HOOK_OUTPUT_KEYS),
                               ({"block": "x", "ask": "y"}, HOOK_OUTPUT_KEYS),
                               ({"block": "x", "tool_input": {}}, HOOK_OUTPUT_KEYS),
                               ({"allow": "yes"}, HOOK_OUTPUT_KEYS), ({"tool_input": "ls"}, HOOK_OUTPUT_KEYS),
                               ({"title": ""}, HOOK_OUTPUT_KEYS), ({"context": "x\0"}, HOOK_OUTPUT_KEYS),
                               ({"ask": "y"}, {"block"})):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                parse_hook_output("PreToolUse", value, allowed)


class NativeTaskHookTests(unittest.IsolatedAsyncioTestCase):
    async def dispatch(self, trigger, output, payload):
        async def hook(event, tool_id, context):
            return output
        event = NATIVE_HOOK_TRIGGERS[trigger]
        bridge = NativeHookBridge({event: [HookMatcher(matcher="build", hooks=[hook])]})
        return await bridge.dispatch({"hook_event_name": event, **payload})

    async def test_spec_task_hooks_add_context_or_block(self):
        payload = {"spec_name": "release", "task_name": "build docs"}
        context = await self.dispatch("preTaskExecution", {"context": "Use the style guide"}, payload)
        self.assertEqual(command_result({"hook_event_name": "PreTaskExec"}, context)["stdout"], "Use the style guide")
        blocked = await self.dispatch("preTaskExecution", {"block": "Not yet"}, payload)
        self.assertEqual(command_result({"hook_event_name": "PreTaskExec"}, blocked)["exitCode"], 2)
        after = await self.dispatch("postTaskExecution", {"context": "ignored"}, {**payload, "task_success": True})
        self.assertTrue(after["block"])

    async def test_agent_stop_continue_maps_to_kiro_stop_output(self):
        response = await self.dispatch("agentStop", {"continue": "Add tests"}, {"task_name": "build"})
        self.assertEqual(command_result({"hook_event_name": "Stop"}, response)["stdout"],
                         '{"decision": "block", "reason": "Add tests"}')


if __name__ == "__main__":
    unittest.main()
