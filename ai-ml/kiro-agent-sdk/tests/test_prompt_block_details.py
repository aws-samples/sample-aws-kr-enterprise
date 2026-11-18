import unittest

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, PromptBlockedError, ProtocolError
from test_transport import FakeTransport


class PromptBlockDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_block_reasons_are_ordered_and_never_embedded_in_exception(self):
        async def first(event, tool_id, context):
            return {'block': "PRIVATE_REASON_1"}
        async def second(event, tool_id, context):
            return {'block': "PRIVATE_REASON_2"}
        transport = FakeTransport()
        options = KiroAgentOptions(agent_engine="v3", hooks={
            "promptSubmit": [HookMatcher(hooks=[first, second])]})
        async with KiroSDKClient(options, transport=transport) as client:
            with self.assertRaises(PromptBlockedError) as caught:
                await client.query("PRIVATE_PROMPT")
            error = caught.exception
            self.assertIsInstance(error, ProtocolError)
            self.assertEqual(error.reasons, ("PRIVATE_REASON_1", "PRIVATE_REASON_2"))
            self.assertEqual(error.reason, "PRIVATE_REASON_1")
            self.assertEqual(error.session_id, "remote")
            self.assertNotIn("PRIVATE_", str(error))
            self.assertNotIn("PRIVATE_", repr(error))
            self.assertNotIn("PRIVATE_", repr(error.args))
        self.assertFalse(any(item["method"] == "session/prompt" for item in transport.requests))

    async def test_block_without_reason_reports_no_reason(self):
        async def allowed(event, tool_id, context):
            return {}
        async def block(event, tool_id, context):
            return {'block': True}
        options = KiroAgentOptions(agent_engine="v3", hooks={
            "promptSubmit": [HookMatcher(hooks=[allowed, block])]})
        async with KiroSDKClient(options, transport=FakeTransport()) as client:
            with self.assertRaises(PromptBlockedError) as caught:
                await client.query("PRIVATE_PROMPT")
            self.assertEqual(caught.exception.reasons, ())
            self.assertIsNone(caught.exception.reason)

    async def test_invalid_hook_outputs_fail_before_prompt(self):
        invalid = [
            {"block": 1},
            {"block": "x" * 8193},
            {"block": "x\0"},
            {"block": "\ud800"},
            {"block": "x", "ask": "y"},
            {"title": ""}, {"context": 1}, {"continue": True}, {"unknown": True},
        ]
        for output in invalid:
            with self.subTest(output=output):
                async def hook(event, tool_id, context):
                    return output
                transport = FakeTransport()
                options = KiroAgentOptions(agent_engine="v3", hooks={
                    "promptSubmit": [HookMatcher(hooks=[hook])]})
                async with KiroSDKClient(options, transport=transport) as client:
                    with self.assertRaises(ProtocolError):
                        await client.query("PRIVATE_PROMPT")
                self.assertFalse(any(item["method"] == "session/prompt" for item in transport.requests))

    async def test_many_hook_reasons_are_bounded(self):
        async def block(event, tool_id, context):
            return {'block': "x"}
        transport = FakeTransport()
        options = KiroAgentOptions(agent_engine="v3", hooks={
            "promptSubmit": [HookMatcher(hooks=[block] * 33)]})
        async with KiroSDKClient(options, transport=transport) as client:
            with self.assertRaisesRegex(ProtocolError, "reasons exceed"):
                await client.query("PRIVATE_PROMPT")
        self.assertFalse(any(item["method"] == "session/prompt" for item in transport.requests))

    async def test_combined_reason_bytes_are_bounded(self):
        async def block(event, tool_id, context):
            return {'block': "가" * 8192}
        transport = FakeTransport()
        options = KiroAgentOptions(agent_engine="v3", hooks={
            "promptSubmit": [HookMatcher(hooks=[block] * 3)]})
        async with KiroSDKClient(options, transport=transport) as client:
            with self.assertRaisesRegex(ProtocolError, "reasons exceed"):
                await client.query("PRIVATE_PROMPT")
        self.assertFalse(any(item["method"] == "session/prompt" for item in transport.requests))

    async def test_allowed_prompt_can_follow_block(self):
        state = {"block": True}
        async def hook(event, tool_id, context):
            return {"block": "policy"} if state["block"] else {"context": "extra"}
        transport = FakeTransport()
        options = KiroAgentOptions(agent_engine="v3", hooks={"promptSubmit": [HookMatcher(hooks=[hook])]})
        async with KiroSDKClient(options, transport=transport) as client:
            with self.assertRaises(PromptBlockedError):
                await client.query("blocked")
            state["block"] = False
            await client.query("allowed")
            _ = [message async for message in client.receive_response()]
        prompt = next(item["params"]["prompt"] for item in transport.requests
                      if item["method"] == "session/prompt")
        self.assertEqual(prompt, [{"type": "text", "text": "allowed"}, {"type": "text", "text": "extra"}])


if __name__ == "__main__":
    unittest.main()
