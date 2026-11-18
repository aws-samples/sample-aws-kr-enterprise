import copy
import json
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport


class ImageTransport(FakeTransport):
    async def write(self, data):
        message = json.loads(data)
        if message.get("method") == "initialize":
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1, "agentCapabilities": {
                    "loadSession": True, "promptCapabilities": {"image": True}}}})
        else:
            await super().write(data)


class ImageInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_image_block_is_sent_unchanged(self):
        block = {"type": "image", "mimeType": "image/png", "data": "AA=="}
        original = copy.deepcopy(block)
        async def messages():
            yield [{"type": "text", "text": "Describe it"}, block]
        transport = ImageTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            await client.query(messages())
            _ = [message async for message in client.receive_response()]
        prompt = next(m["params"]["prompt"] for m in transport.requests if m["method"] == "session/prompt")
        self.assertEqual(prompt, [{"type": "text", "text": "Describe it"}, original])
        self.assertEqual(block, original)

    async def test_unadvertised_capability_is_rejected_before_prompt(self):
        block = {"type": "image", "mimeType": "image/png", "data": "AA=="}
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            with self.assertRaises(ProtocolError):
                await client.query([{"type": "text", "text": "Describe"}, block])
        self.assertFalse(any(m["method"] == "session/prompt" for m in transport.requests))

    async def test_invalid_image_blocks_fail_explicitly(self):
        blocks = [
            {"type": "image", "mimeType": "image/png", "data": "%%"},
            {"type": "image", "mimeType": "application/pdf", "data": "AA=="},
            {"type": "image", "mimeType": "image/png"},
            {"type": "image", "mimeType": "image/png", "data": "AA==", "cache_control": {"type": "ephemeral"}},
        ]
        transport = ImageTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            for block in blocks:
                with self.subTest(block=block), self.assertRaises(ValueError):
                    await client.query([block])
        self.assertFalse(any(m["method"] == "session/prompt" for m in transport.requests))

    async def test_native_acp_image_is_preserved(self):
        block = {"type": "image", "mimeType": "image/png", "data": "AA=="}
        transport = ImageTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            await client.query([{"type": "text", "text": "Describe"}, block])
            _ = [message async for message in client.receive_response()]
        prompt = next(m["params"]["prompt"] for m in transport.requests if m["method"] == "session/prompt")
        self.assertEqual(prompt[1], block)


if __name__ == "__main__":
    unittest.main()
