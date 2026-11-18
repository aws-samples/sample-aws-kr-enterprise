import json
import unittest

from kiro_agent_sdk import (KiroAgentOptions, KiroSDKClient, UserMessage)
from test_image_input import ImageTransport


class ReplayTransport(ImageTransport):
    def __init__(self, updates=()):
        super().__init__()
        self.updates = updates

    async def write(self, data):
        message = json.loads(data)
        if message["method"] == "session/prompt":
            for value in self.updates:
                await self.queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "remote", "update": {"sessionUpdate": "session_info_update", "_meta": {
                        "kiro": {"kind": "user_message_id_assigned", **value}}}}})
        await super().write(data)


class UserMessageReplayTests(unittest.IsolatedAsyncioTestCase):
    def options(self, **kwargs):
        return KiroAgentOptions(agent_engine="v3", extra_args={"replay-user-messages": None}, **kwargs)

    async def messages(self, transport, prompt, **kwargs):
        async with KiroSDKClient(self.options(**kwargs), transport=transport) as client:
            await client.query(prompt)
            return [message async for message in client.receive_response()]

    async def test_default_does_not_emit_a_replayed_user_message(self):
        transport = ReplayTransport([{"userMessageId": "user-123"}])
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            await client.query("hello")
            messages = [message async for message in client.receive_response()]
        self.assertFalse(any(isinstance(message, UserMessage) for message in messages))
        self.assertEqual(messages[-1].user_message_id, "user-123")


if __name__ == "__main__":
    unittest.main()
