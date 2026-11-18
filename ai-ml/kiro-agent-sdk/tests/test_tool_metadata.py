import json
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, PermissionResultAllow, KiroEvent, ToolCall
from test_transport import FakeTransport
from test_user_interactions import InteractionTransport


class ToolMetadataTransport(FakeTransport):
    async def write(self, data):
        message = json.loads(data)
        if message.get("method") == "session/prompt":
            self.requests.append(message)
            updates = [
                {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Display title",
                 "_meta": {"kiro": {"toolOrigin": {"type": "builtin"}, "originFlag": True}}},
                {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "rawInput": {"path": "x"},
                 "_meta": {"kiro": {"toolId": "read_file"}}},
                {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
                 "rawOutput": "ok", "_meta": {"kiro": {"other": "value"}}},
            ]
            for update in updates:
                await self.queue.put({"jsonrpc": "2.0", "method": "session/update",
                                      "params": {"sessionId": "remote", "update": update}})
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "end_turn"}})
        else:
            await super().write(data)


class ToolMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_blocks_and_results_preserve_reported_metadata(self):
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=ToolMetadataTransport()) as client:
            await client.query("start")
            messages = [m async for m in client.receive_response()]
        block, result = [m for m in messages if isinstance(m, ToolCall)]
        self.assertEqual(block.kiro_metadata["toolOrigin"], {"type": "builtin"})
        self.assertEqual(block.kiro_metadata["toolId"], "read_file")
        self.assertEqual(result.kiro_metadata["originFlag"], True)
        self.assertEqual(result.kiro_metadata["other"], "value")
        block.kiro_metadata["toolOrigin"]["type"] = "modified"
        first = next(m for m in messages if isinstance(m, KiroEvent) and m.kind == "tool_call")
        self.assertEqual(first.data["_meta"]["kiro"]["toolOrigin"]["type"], "builtin")

    async def test_subagent_tool_metadata_survives_partial_updates(self):
        class ChildTransport(FakeTransport):
            async def write(self, data):
                message = json.loads(data)
                if message.get("method") == "session/prompt":
                    updates = [
                        {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read",
                         "_meta": {"kiro": {"agentSubtaskId": "child", "toolOrigin": {"type": "builtin"}}}},
                        {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "rawInput": {"path": "x"},
                         "_meta": {"kiro": {"toolId": "read_file"}}},
                    ]
                    for update in updates:
                        await self.queue.put({"jsonrpc": "2.0", "method": "session/update",
                                              "params": {"sessionId": "remote", "update": update}})
                    await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "end_turn"}})
                else:
                    await super().write(data)
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=ChildTransport()) as client:
            await client.query("start")
            messages = [m async for m in client.receive_response()]
        block = next(m for m in messages if isinstance(m, ToolCall))
        self.assertEqual(block.agent_id, "child")
        self.assertEqual(block.kiro_metadata["agentSubtaskId"], "child")
        self.assertEqual(block.kiro_metadata["toolId"], "read_file")

    async def test_permission_context_exposes_copy_of_kiro_metadata(self):
        params = {"sessionId": "remote", "toolCall": {
            "title": "display", "_meta": {"kiro": {"mcpTool": {"identity": {"serverName": "probe"}}}}},
            "options": [{"kind": "allow_once", "optionId": "yes"}]}
        async def approve(name, data, context):
            metadata = context.kiro_metadata
            self.assertEqual(metadata["mcpTool"]["identity"]["serverName"], "probe")
            metadata["mcpTool"]["identity"]["serverName"] = "modified"
            self.assertEqual(context.tool_call["_meta"]["kiro"]["mcpTool"]["identity"]["serverName"], "probe")
            return PermissionResultAllow()
        transport = InteractionTransport("session/request_permission", params)
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=approve),
                                 transport=transport) as client:
            await client.query("start")
            _ = [m async for m in client.receive_response()]
        self.assertEqual(transport.responses[0]["result"]["outcome"]["optionId"], "yes")


if __name__ == "__main__":
    unittest.main()
