import json
import unittest
from pathlib import Path

from kiro_agent_sdk import (HookMatcher, KiroAgentOptions, KiroSDKClient, PermissionResultAllow,
                            PermissionResultDeny)


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


async def run(options):
    async with KiroSDKClient(options) as client:
        await client.query("permission_consent")
        result = [message async for message in client.receive_response()][-1]
        return json.loads(result.result)


class PermissionReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_denial_message_sent_only_for_v3_once_choice(self):
        async def deny(name, input_data, context):
            return PermissionResultDeny(message="Reason from application")

        v3 = await run(KiroAgentOptions(cli_path=FAKE, agent_engine="v3", can_use_tool=deny))
        self.assertEqual(v3["_meta"]["kiro"]["rejectionReason"], "Reason from application")
        v2 = await run(KiroAgentOptions(cli_path=FAKE, agent_engine="v2", can_use_tool=deny))
        self.assertNotIn("_meta", v2)

        async def persist(name, input_data, context):
            return PermissionResultDeny(message="Not forwarded for persistent rejection", option_id="always-no")
        persistent = await run(KiroAgentOptions(cli_path=FAKE, agent_engine="v3", can_use_tool=persist))
        self.assertNotIn("_meta", persistent)

    async def test_hook_denial_reason_overrides_callback_allow(self):
        async def block(event, tool_id, context):
            return {'block': "Reason from hook"}

        async def allow(name, input_data, context):
            return PermissionResultAllow()

        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", can_use_tool=allow,
                                   hooks={"preToolUse": [HookMatcher(hooks=[block])]})
        response = await run(options)
        self.assertEqual(response["outcome"]["optionId"], "no")
        self.assertEqual(response["_meta"]["kiro"]["rejectionReason"], "Reason from hook")

    async def test_hook_ask_reason_is_provided_to_permission_callback(self):
        async def ask(event, tool_id, context):
            return {'ask': "Check this operation"}

        async def deny(name, input_data, context):
            self.assertEqual(context.decision_reason, "Check this operation")
            return PermissionResultDeny(message="Application declined")

        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", can_use_tool=deny,
                                   hooks={"preToolUse": [HookMatcher(hooks=[ask])]})
        response = await run(options)
        self.assertEqual(response["_meta"]["kiro"]["rejectionReason"], "Application declined")

    async def test_legacy_hook_block_reason_reaches_kiro(self):
        async def block(event, tool_id, context):
            return {'block': "Restricted by hook"}

        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
                                   hooks={"preToolUse": [HookMatcher(hooks=[block])]})
        response = await run(options)
        self.assertEqual(response["_meta"]["kiro"]["rejectionReason"], "Restricted by hook")


if __name__ == "__main__":
    unittest.main()
