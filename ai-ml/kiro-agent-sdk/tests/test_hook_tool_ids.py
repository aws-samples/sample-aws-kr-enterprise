import unittest
from pathlib import Path

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient


FAKE = Path(__file__).with_name("fake_kiro.py").resolve()


class HookToolIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_tool_id_is_in_input_and_callback_argument(self):
        seen = []

        async def observe(event, tool_call_id, context):
            seen.append((event["hook_event_name"], event.get("tool_call_id"), tool_call_id))
            return {}

        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
                                   hooks={"preToolUse": [HookMatcher(hooks=[observe])],
                                          "postToolUse": [HookMatcher(hooks=[observe])]})
        async with KiroSDKClient(options) as client:
            await client.query("permission")
            _ = [message async for message in client.receive_response()]
            await client.query("nested")
            _ = [message async for message in client.receive_response()]
        self.assertIn(("PreToolUse", "call-1", "call-1"), seen)
        self.assertIn(("PostToolUse", "child-tool", "child-tool"), seen)
        self.assertIn(("PostToolUse", "spawn", "spawn"), seen)


if __name__ == "__main__":
    unittest.main()
