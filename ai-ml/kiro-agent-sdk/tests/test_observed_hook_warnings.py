import asyncio
import json
import unittest

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError, KiroEvent
from test_transport import FakeTransport
from test_inbound_cancellation import PermissionTransport
from test_user_interactions import InteractionTransport


class EventTransport(FakeTransport):
    async def write(self, data):
        message = json.loads(data)
        if message.get("method") != "session/prompt":
            await super().write(data)
            return
        self.requests.append(message)
        child = {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "child"}}
        updates = [
            {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "probe", "rawInput": {}},
            {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed", "rawOutput": "ok"},
            {"sessionUpdate": "tool_call", "toolCallId": "t2", "title": "probe", "rawInput": {}},
            {"sessionUpdate": "tool_call_update", "toolCallId": "t2", "status": "failed", "rawOutput": "failed"},
            {"sessionUpdate": "tool_call", "toolCallId": "spawn", "status": "in_progress", "_meta": child},
            {"sessionUpdate": "tool_call_update", "toolCallId": "spawn", "status": "completed", "_meta": child},
        ]
        for update in updates:
            await self.queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "remote", "update": update}})
        await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "end_turn"}})


def warnings(messages):
    return [(message.data["hook_event_name"], message.data["message"]) for message in messages
            if isinstance(message, KiroEvent) and message.kind == "hook_warning"]


class ObservedHookWarningTests(unittest.IsolatedAsyncioTestCase):

    async def test_other_session_permission_warning_is_not_misattributed(self):
        async def hook(event, tool_id, context):
            return {'warning': "OTHER_SESSION_WARNING"}
        params = {"sessionId": "other", "toolCall": {"title": "write", "rawInput": {}},
                  "options": [{"kind": "reject_once", "optionId": "no"}]}
        transport = InteractionTransport("session/request_permission", params)
        options = KiroAgentOptions(agent_engine="v3", hooks={"preToolUse": [HookMatcher(hooks=[hook])]})
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query("prompt")
            messages = [message async for message in client.receive_response()]
        self.assertEqual(warnings(messages), [])

    async def test_invalid_permission_warning_rejects_request(self):
        async def hook(event, tool_id, context):
            return {'warning': "x" * 8193}
        params = {"sessionId": "remote", "toolCall": {"title": "write", "rawInput": {}},
                  "options": [{"kind": "allow_once", "optionId": "yes"}]}
        transport = InteractionTransport("session/request_permission", params)
        options = KiroAgentOptions(agent_engine="v3", hooks={"preToolUse": [HookMatcher(hooks=[hook])]})
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query("prompt")
            with self.assertRaises(ProtocolError):
                _ = [message async for message in client.receive_response()]
        self.assertNotEqual(transport.responses[0].get("result", {}).get("outcome", {}).get("optionId"), "yes")

    async def test_stop_warning_count_is_bounded(self):
        async def hook(event, tool_id, context):
            return {'warning': "warning"}
        options = KiroAgentOptions(agent_engine="v3", hooks={"agentStop": [HookMatcher(hooks=[hook] * 33)]})
        async with KiroSDKClient(options, transport=FakeTransport()) as client:
            await client.query("prompt")
            with self.assertRaisesRegex(ProtocolError, "system messages exceed"):
                _ = [message async for message in client.receive_response()]

    async def test_cancelled_permission_warning_never_reaches_later_turn(self):
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def hook(event, tool_id, context):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'warning': "LATE_WARNING"}
        transport = PermissionTransport()
        options = KiroAgentOptions(agent_engine="v3", hooks={"preToolUse": [HookMatcher(hooks=[hook])]})
        try:
            async with KiroSDKClient(options, transport=transport) as client:
                await client.query("first")
                await asyncio.wait_for(started.wait(), 2)
                await transport.queue.put({"jsonrpc": "2.0", "id": transport.prompt_id,
                                           "result": {"stopReason": "end_turn"}})
                first = [message async for message in client.receive_response()]
                release.set()
                await asyncio.wait_for(finished.wait(), 2)
                await client.query("second")
                second = [message async for message in client.receive_response()]
                self.assertEqual(warnings(first + second), [])
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
