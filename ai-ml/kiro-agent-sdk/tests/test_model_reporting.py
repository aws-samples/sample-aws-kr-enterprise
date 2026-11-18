import json
import unittest

from kiro_agent_sdk import AgentMessage, KiroAgentOptions, KiroSDKClient
from test_transport import FakeTransport


class DelayedModelTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.first_prompt = None

    async def write(self, data):
        message = json.loads(data)
        if message.get("method") == "session/prompt" and self.first_prompt is None:
            self.requests.append(message)
            self.first_prompt = message["id"]
        elif message.get("method") == "session/set_config_option":
            await super().write(data)
            await self.queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "remote", "update": {"sessionUpdate": "agent_message_chunk",
                                                    "content": {"type": "text", "text": "first"}}}})
            await self.queue.put({"jsonrpc": "2.0", "id": self.first_prompt, "result": {"stopReason": "end_turn"}})
        else:
            await super().write(data)


class ModelReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_model_is_frozen_per_turn(self):
        transport = DelayedModelTransport()
        options = KiroAgentOptions(agent_engine="v3")
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query("first")
            await client.set_config_option("model", "next-model")
            first = [m async for m in client.receive_response()]
            await client.query("second")
            second = [m async for m in client.receive_response()]
        self.assertEqual(next(m for m in first if isinstance(m, AgentMessage)).model, "auto")
        self.assertEqual(next(m for m in second if isinstance(m, AgentMessage)).model, "next-model")
        self.assertIsNone(options.model)


if __name__ == "__main__":
    unittest.main()
