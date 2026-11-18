import asyncio
import json
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ProtocolError, ResultMessage
from test_transport import FakeTransport


class InteractionTransport(FakeTransport):
    def __init__(self, method, params):
        super().__init__()
        self.method = method
        self.params = params
        self.responses = []
        self.prompt_id = None

    async def write(self, data):
        message = json.loads(data)
        if message.get("method") == "session/prompt":
            self.requests.append(message)
            self.prompt_id = message["id"]
            await self.queue.put({"jsonrpc": "2.0", "id": "interaction", "method": self.method,
                                  "params": self.params})
        elif "method" not in message:
            self.responses.append(message)
            await self.queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "remote", "update": {"sessionUpdate": "agent_message_chunk",
                                                    "content": {"type": "text", "text": "done"}}}})
            await self.queue.put({"jsonrpc": "2.0", "id": self.prompt_id, "result": {"stopReason": "end_turn"}})
        else:
            await super().write(data)


class UserInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_input_negotiation_and_answer(self):
        received = []
        params = {"sessionId": "remote", "toolCallId": "u1", "question": "Color?",
                  "options": [{"title": "purple", "recommended": True}]}
        async def ask(request):
            received.append(request)
            return "purple"
        transport = InteractionTransport("_kiro/userInput", params)
        options = KiroAgentOptions(agent_engine="v3", on_user_input=ask)
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query("start")
            messages = [m async for m in client.receive_response()]
        self.assertEqual(received, [params])
        self.assertIsInstance(messages[-1], ResultMessage)
        self.assertEqual(transport.responses[0]["result"], {"action": "answered", "answer": "purple"})
        init = next(r for r in transport.requests if r.get("method") == "initialize")
        self.assertIs(init["params"]["clientCapabilities"]["_meta"]["kiro"]["userInput"], True)

    async def test_dismissal_and_default_cancel(self):
        async def dismiss(_request):
            return None
        params = {"question": "What next?", "options": []}
        transport = InteractionTransport("_kiro/userInput", params)
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", on_user_input=dismiss),
                                 transport=transport) as client:
            await client.query("start")
            _ = [m async for m in client.receive_response()]
        self.assertEqual(transport.responses[0]["result"], {"action": "dismissed"})
        transport = InteractionTransport("_kiro/mcp/elicitation", {"elicitation": {}})
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            await client.query("start")
            _ = [m async for m in client.receive_response()]
        self.assertEqual(transport.responses[0]["result"], {"action": "cancel"})
        init = next(r for r in transport.requests if r.get("method") == "initialize")
        self.assertNotIn("userInput", init["params"]["clientCapabilities"]["_meta"]["kiro"])

    async def test_mcp_elicitation_accepts_form_response(self):
        received = []
        params = {"sessionId": "remote", "toolCallId": "m1",
                  "elicitation": {"mode": "form", "message": "Name?",
                                  "requestedSchema": {"type": "object"}}}
        async def elicit(request):
            received.append(request)
            return {"action": "accept", "content": {"name": "Mina"}}
        transport = InteractionTransport("_kiro/mcp/elicitation", params)
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", on_mcp_elicitation=elicit),
                                 transport=transport) as client:
            await client.query("start")
            _ = [m async for m in client.receive_response()]
        self.assertEqual(received, [params])
        self.assertEqual(transport.responses[0]["result"], {"action": "accept", "content": {"name": "Mina"}})

    async def test_invalid_callback_response_surfaces_and_stops_turn(self):
        async def invalid(_request):
            return {"action": "accept", "content": float("nan")}
        transport = InteractionTransport("_kiro/mcp/elicitation", {"elicitation": {}})
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", on_mcp_elicitation=invalid),
                                 transport=transport) as client:
            await client.query("start")
            with self.assertRaisesRegex(ProtocolError, "interaction callback failed"):
                _ = [m async for m in client.receive_response()]
        self.assertTrue(transport.closed)

    async def test_disconnect_cancels_waiting_callback(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def wait_for_user(_request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        transport = InteractionTransport("_kiro/userInput", {"question": "?", "options": []})
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3", on_user_input=wait_for_user), transport=transport)
        await client.connect()
        await client.query("start")
        await asyncio.wait_for(started.wait(), 2)
        await client.disconnect()
        await asyncio.wait_for(cancelled.wait(), 2)

    def test_callbacks_require_v3(self):
        async def callback(_request):
            return None
        for field in ("on_user_input", "on_mcp_elicitation"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                KiroSDKClient(KiroAgentOptions(agent_engine="v2", **{field: callback}))


if __name__ == "__main__":
    unittest.main()
