"""Exact wire requests and returned values for checkpoint, session delete and MCP switch."""
import json
import unittest

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport


class CapabilityTransport(FakeTransport):
    async def write(self, data):
        message = json.loads(data)
        method = message.get("method")
        if "id" not in message:
            self.requests.append(message)
        elif method == "initialize":
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1, "agentCapabilities": {
                    "loadSession": True, "sessionCapabilities": {"delete": {}},
                    "_meta": {"kiro": {"checkpoints": True}}}}})
        elif method == "_kiro/checkpoint/revertMultiple":
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                "success": True, "affectedFiles": ["a.txt"], "futureField": ["preserved"]}})
        elif method == "session/delete":
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {"ignored": True}})
        else:
            await super().write(data)


class ControlResultContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_checkpoint_sends_exact_request_and_returns_the_result_object(self):
        transport = CapabilityTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            result = await client.restore_checkpoint("user-1")
            self.assertEqual((transport.requests[-1]["method"], transport.requests[-1]["params"]),
                             ("_kiro/checkpoint/revertMultiple", {"sessionId": "remote", "messageId": "user-1"}))
            self.assertEqual(result, {"success": True, "affectedFiles": ["a.txt"], "futureField": ["preserved"]})
            before = len(transport.requests)
            for value in ("", "  ", None):
                with self.subTest(message_id=value), self.assertRaises(ValueError):
                    await client.restore_checkpoint(value)
            self.assertEqual(len(transport.requests), before)

    async def test_delete_session_sends_exact_request_returns_none_and_protects_active_session(self):
        transport = CapabilityTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            self.assertIsNone(await client.delete_session("stored"))
            self.assertEqual((transport.requests[-1]["method"], transport.requests[-1]["params"]),
                             ("session/delete", {"sessionId": "stored"}))
            before = len(transport.requests)
            with self.assertRaises(ValueError):
                await client.delete_session("remote")
            self.assertEqual(len(transport.requests), before)

    async def test_kiro_mcp_switch_is_one_unacknowledged_notification_returning_none(self):
        transport = CapabilityTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            for enabled in (False, True):
                self.assertIsNone(await client.request_kiro_mcp_enabled(enabled))
                self.assertEqual(transport.requests[-1], {"jsonrpc": "2.0", "method": "_kiro/mcp/toggle",
                                                          "params": {"enabled": enabled}})
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=CapabilityTransport())
        with self.assertRaises(CLIConnectionError):
            await client.request_kiro_mcp_enabled(True)
        with self.assertRaises(ProtocolError):
            await KiroSDKClient(KiroAgentOptions(agent_engine="v2"),
                                transport=CapabilityTransport()).request_kiro_mcp_enabled(True)


if __name__ == "__main__":
    unittest.main()
