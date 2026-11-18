import unittest

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, PermissionResultAllow, PermissionResultDeny, ProtocolError
from test_user_interactions import InteractionTransport


class PreToolHookDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def decision(self, decisions, *, mode="ask", callback=None):
        hooks = []
        for value in decisions:
            async def hook(event, tool_id, context, decision=value):
                outputs = {"allow": {"allow": True}, "deny": {"block": "test"}, "ask": {"ask": "test"}}
                return outputs.get(decision, {"defer": True})
            hooks.append(hook)
        params = {"sessionId": "remote", "toolCall": {"toolCallId": "t1", "title": "tool", "rawInput": {}},
                  "options": [{"kind": "allow_once", "optionId": "yes"},
                              {"kind": "reject_once", "optionId": "no"}]}
        transport = InteractionTransport("session/request_permission", params)
        options = KiroAgentOptions(agent_engine="v3", permission_mode=mode, can_use_tool=callback,
                                   hooks={"preToolUse": [HookMatcher(hooks=hooks)]})
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query("start")
            if "defer" in decisions:
                with self.assertRaisesRegex(ProtocolError, "permission callback or hook failed"):
                    _ = [m async for m in client.receive_response()]
            else:
                _ = [m async for m in client.receive_response()]
        return transport.responses[0]

    async def test_allow_and_deny(self):
        self.assertEqual((await self.decision(["allow"]))["result"]["outcome"]["optionId"], "yes")
        self.assertEqual((await self.decision(["deny"], mode="trustAll"))["result"]["outcome"]["optionId"], "no")
        self.assertEqual((await self.decision(["allow", "deny"]))["result"]["outcome"]["optionId"], "no")

    async def test_ask_requires_callback_and_callback_deny_wins(self):
        self.assertEqual((await self.decision(["ask"], mode="trustAll"))["result"]["outcome"]["optionId"], "no")
        async def allow(*args):
            return PermissionResultAllow()
        async def deny(*args):
            return PermissionResultDeny()
        self.assertEqual((await self.decision(["ask"], callback=allow))["result"]["outcome"]["optionId"], "yes")
        self.assertEqual((await self.decision(["allow"], callback=deny))["result"]["outcome"]["optionId"], "no")

    async def test_unsupported_defer_fails_the_request(self):
        result = await self.decision(["defer"])
        self.assertEqual(result["error"]["code"], -32603)

    async def test_permission_callback_failure_is_reported_to_caller(self):
        async def fail(*args):
            raise RuntimeError("synthetic callback failure")
        params = {"sessionId": "remote", "toolCall": {"title": "tool"},
                  "options": [{"kind": "allow_once", "optionId": "yes"}]}
        transport = InteractionTransport("session/request_permission", params)
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=fail),
                                 transport=transport) as client:
            await client.query("start")
            with self.assertRaisesRegex(ProtocolError, "permission callback or hook failed"):
                _ = [m async for m in client.receive_response()]
        self.assertEqual(transport.responses[0]["error"]["code"], -32603)


if __name__ == "__main__":
    unittest.main()
