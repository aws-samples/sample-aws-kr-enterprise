import asyncio
import json
import unittest
from pathlib import Path

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, PermissionResultAllow
from test_inbound_cancellation import PermissionTransport

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class ReportedPermissionTransport(PermissionTransport):
    def __init__(self, params, updates=()):
        super().__init__()
        self.params = params
        self.updates = updates

    async def write(self, data):
        message = json.loads(data)
        if message.get("method") == "session/prompt":
            self.messages.append(message)
            self.prompt_id = message["id"]
            for params in self.updates:
                await self.queue.put({"jsonrpc": "2.0", "method": "session/update", "params": params})
            await self.queue.put({"jsonrpc": "2.0", "id": 100, "method": "session/request_permission",
                                  "params": self.params})
            return
        await super().write(data)


class ReportedPermissionContextTests(unittest.IsolatedAsyncioTestCase):
    def params(self, *, session_id="s", tool_metadata=None, request_metadata=None):
        tool = {"toolCallId": "tool-1", "title": "write", "rawInput": {"path": "example"},
                "displayName": "Write file", "description": "Approval requested"}
        if tool_metadata is not None:
            tool["_meta"] = {"kiro": tool_metadata}
        params = {"sessionId": session_id, "toolCall": tool,
                  "options": [{"kind": "allow_once", "optionId": "yes"},
                              {"kind": "reject_once", "optionId": "no"}]}
        if request_metadata is not None:
            params["_meta"] = {"kiro": request_metadata}
        return params

    async def observe(self, params, updates=()):
        hooks, contexts = [], []
        async def hook(event, tool_id, context):
            hooks.append(event)
            return {}
        async def decide(name, data, context):
            contexts.append(context)
            return PermissionResultAllow()
        options = KiroAgentOptions(agent_engine="v3", can_use_tool=decide,
            hooks={"preToolUse": [HookMatcher(hooks=[hook])],
                   "PermissionRequest": [HookMatcher(hooks=[hook])]})
        async with KiroSDKClient(options, transport=ReportedPermissionTransport(params, updates)) as client:
            await client.query("first")
            _ = [item async for item in client.receive_response()]
        return hooks, contexts[0]

    @staticmethod
    def spawn(agent_id="child-1", name="reviewer", tool_id="spawn", session_id="s"):
        return {"sessionId": session_id, "update": {"sessionUpdate": "tool_call", "toolCallId": tool_id,
            "rawInput": {"name": name}, "_meta": {"kiro": {"kind": "agent-subtask",
                                                            "agentSubtaskId": agent_id}}}}

    async def wait_for_type(self, client):
        async def wait():
            while not client._subagent_types_by_session.get(client.session_id):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), 5)

    async def test_agent_type_is_not_carried_into_a_later_turn_without_a_new_report(self):
        seen = []
        async def decide(name, data, context):
            seen.append(context.kiro_agent_type)
            return PermissionResultAllow()
        transport = ReportedPermissionTransport(self.params(tool_metadata={"agentSubtaskId": "child-1"}),
                                                 [self.spawn()])
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=decide),
                                 transport=transport) as client:
            await client.query("first")
            _ = [item async for item in client.receive_response()]
            transport.updates = []
            await client.query("second")
            _ = [item async for item in client.receive_response()]
        self.assertEqual(seen, ["reviewer", None])

    async def test_posttool_hooks_report_child_not_parent_spawn(self):
        seen = []
        async def hook(event, tool_id, context):
            seen.append((tool_id, event.get("agent_id"), event.get("agent_type"),
                         event.get("kiro_observed_agent_type")))
            return {}
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
                                   hooks={"postToolUse": [HookMatcher(hooks=[hook])]})
        async with KiroSDKClient(options) as client:
            await client.query("nested")
            _ = [item async for item in client.receive_response()]
        self.assertIn(("child-tool", "child-1", "child-type", True), seen)
        self.assertIn(("spawn", None, None, None), seen)
