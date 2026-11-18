import unittest

from kiro_agent_sdk import (HookMatcher, KiroAgentOptions, KiroSDKClient, PromptBlockedError,
                            ProtocolError, ResultMessage, KiroEvent)
from test_transport import FakeTransport


class PromptHookWarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_warnings_are_ordered_events_and_never_sent_to_model(self):
        state = {"warn": True}
        async def first(event, tool_id, context):
            return {"warning": "PRIVATE_WARNING_1"} if state["warn"] else {}
        async def second(event, tool_id, context):
            return {"warning": "PRIVATE_WARNING_2"} if state["warn"] else {}
        options = KiroAgentOptions(agent_engine="v3", hooks={
            "promptSubmit": [HookMatcher(hooks=[first, second])]})
        transport = FakeTransport()
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query("first prompt")
            messages = [message async for message in client.receive_response()]
            warnings = [message for message in messages
                        if isinstance(message, KiroEvent) and message.kind == "hook_warning"]
            self.assertEqual([item.data for item in warnings], [
                {"hook_event_name": "UserPromptSubmit", "message": "PRIVATE_WARNING_1", "session_id": "remote"},
                {"hook_event_name": "UserPromptSubmit", "message": "PRIVATE_WARNING_2", "session_id": "remote"},
            ])
            self.assertLess(messages.index(warnings[-1]),
                            next(index for index, message in enumerate(messages) if isinstance(message, ResultMessage)))
            state["warn"] = False
            await client.query("second prompt")
            later = [message async for message in client.receive_response()]
            self.assertFalse(any(isinstance(item, KiroEvent) and item.kind == "hook_warning"
                                 for item in later))
        prompts = [item["params"]["prompt"] for item in transport.requests if item["method"] == "session/prompt"]
        self.assertEqual(prompts, [[{"type": "text", "text": "first prompt"}],
                                   [{"type": "text", "text": "second prompt"}]])
        self.assertNotIn("PRIVATE_WARNING", repr(transport.requests))

    async def test_blocked_prompt_exposes_warnings_only_on_exception_attributes(self):
        async def hook(event, tool_id, context):
            return {'block': "PRIVATE_REASON", 'warning': "PRIVATE_WARNING"}
        transport = FakeTransport()
        options = KiroAgentOptions(agent_engine="v3", hooks={"promptSubmit": [HookMatcher(hooks=[hook])]})
        async with KiroSDKClient(options, transport=transport) as client:
            with self.assertRaises(PromptBlockedError) as caught:
                await client.query("PRIVATE_PROMPT")
        self.assertEqual(caught.exception.warnings, ("PRIVATE_WARNING",))
        self.assertEqual(caught.exception.reason, "PRIVATE_REASON")
        self.assertNotIn("PRIVATE_", str(caught.exception))
        self.assertNotIn("PRIVATE_", repr(caught.exception))
        self.assertFalse(any(item["method"] == "session/prompt" for item in transport.requests))

    async def test_invalid_and_excessive_warnings_fail_before_sending(self):
        for warning in (1, "x" * 8193, "\0", "\ud800"):
            with self.subTest(warning=warning):
                async def hook(event, tool_id, context):
                    return {'warning': warning}
                transport = FakeTransport()
                options = KiroAgentOptions(agent_engine="v3", hooks={
                    "promptSubmit": [HookMatcher(hooks=[hook])]})
                async with KiroSDKClient(options, transport=transport) as client:
                    with self.assertRaises(ProtocolError):
                        await client.query("PRIVATE_PROMPT")
                self.assertFalse(any(item["method"] == "session/prompt" for item in transport.requests))
        for text, count in (("x", 33), ("가" * 8192, 3)):
            async def hook(event, tool_id, context):
                return {'warning': text}
            transport = FakeTransport()
            options = KiroAgentOptions(agent_engine="v3", hooks={
                "promptSubmit": [HookMatcher(hooks=[hook] * count)]})
            async with KiroSDKClient(options, transport=transport) as client:
                with self.assertRaisesRegex(ProtocolError, "system messages exceed"):
                    await client.query("PRIVATE_PROMPT")
            self.assertFalse(any(item["method"] == "session/prompt" for item in transport.requests))


if __name__ == "__main__":
    unittest.main()
