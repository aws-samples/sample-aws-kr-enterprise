import unittest

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport


class PromptHookContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_is_appended_in_hook_order(self):
        async def first(event, tool_id, context):
            return {'context': "first"}
        async def second(event, tool_id, context):
            return {'context': "second"}
        transport = FakeTransport()
        options = KiroAgentOptions(agent_engine="v3", hooks={
            "promptSubmit": [HookMatcher(hooks=[first, second])]})
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query("hello")
            _ = [message async for message in client.receive_response()]
        prompt = next(m["params"]["prompt"] for m in transport.requests if m["method"] == "session/prompt")
        self.assertEqual(prompt, [{"type": "text", "text": "hello"}, {"type": "text", "text": "first"},
                                  {"type": "text", "text": "second"}])

    async def test_invalid_context_does_not_send_prompt(self):
        async def invalid(event, tool_id, context):
            return {'context': 5}
        transport = FakeTransport()
        options = KiroAgentOptions(agent_engine="v3", hooks={"promptSubmit": [HookMatcher(hooks=[invalid])]})
        async with KiroSDKClient(options, transport=transport) as client:
            with self.assertRaises(ProtocolError):
                await client.query("hello")
        self.assertFalse(any(m["method"] == "session/prompt" for m in transport.requests))

    async def test_unsupported_hook_fields_fail(self):
        async def unsupported(event, tool_id, context):
            return {'title': "x"}
        options = KiroAgentOptions(agent_engine="v3", hooks={"promptSubmit": [HookMatcher(hooks=[unsupported])]})
        async with KiroSDKClient(options, transport=FakeTransport()) as client:
            with self.assertRaises(ProtocolError):
                await client.query("hello")


if __name__ == "__main__":
    unittest.main()
