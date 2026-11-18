import copy
import json
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport


class DocumentTransport(FakeTransport):
    async def write(self, data):
        message = json.loads(data)
        if message.get("method") == "initialize":
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1, "agentCapabilities": {
                    "loadSession": True, "promptCapabilities": {"embeddedContext": True}}}})
        else:
            await super().write(data)


class DocumentInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedded_resources_are_sent_without_mutating_source(self):
        blocks = [
            {"type": "resource", "resource": {"uri": "memory://report.pdf", "mimeType": "application/pdf",
                                              "blob": "JVBERg=="}},
            {"type": "resource", "resource": {"uri": "memory://notes.txt", "mimeType": "text/plain",
                                              "text": "release notes"}},
        ]
        original = copy.deepcopy(blocks)
        transport = DocumentTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            await client.query([{"type": "text", "text": "Summarize"}, *blocks])
            _ = [message async for message in client.receive_response()]
        prompt = next(m["params"]["prompt"] for m in transport.requests if m["method"] == "session/prompt")
        self.assertEqual(prompt[1:], original)
        self.assertEqual(blocks, original)

    async def test_unadvertised_embedded_context_fails_before_request(self):
        block = {"type": "resource", "resource": {"uri": "memory://notes.txt", "text": "notes"}}
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            with self.assertRaises(ProtocolError):
                await client.query([{"type": "text", "text": "Read"}, block])
        self.assertFalse(any(m["method"] == "session/prompt" for m in transport.requests))

    async def test_invalid_resource_blocks_fail(self):
        blocks = [
            {"type": "resource", "resource": {"text": "no uri"}},
            {"type": "resource", "resource": {"uri": "memory://x", "text": "a", "blob": "AA=="}},
            {"type": "resource", "resource": {"uri": "memory://x", "blob": "%%"}},
            {"type": "resource", "resource": {"uri": "memory://x", "text": 1}},
            {"type": "resource", "resource": {"uri": "memory://x", "text": "a"}, "extra": True},
        ]
        transport = DocumentTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            for block in blocks:
                with self.subTest(block=block), self.assertRaises(ValueError):
                    await client.query([block])
        self.assertFalse(any(m["method"] == "session/prompt" for m in transport.requests))


if __name__ == "__main__":
    unittest.main()
