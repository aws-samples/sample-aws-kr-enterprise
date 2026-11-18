import json
import unittest
from pathlib import Path

from kiro_agent_sdk import (HookMatcher, KiroAgentOptions, KiroSDKClient, PermissionResultAllow,
                            PermissionResultDeny, ProtocolError)


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class PermissionConsentTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_workspace_allow_and_user_deny(self):
        for allow in (True, False):
            with self.subTest(allow=allow):
                async def decide(name, input_data, context):
                    observed = context.kiro_consent
                    self.assertEqual(observed["capability"], "fs_write")
                    observed["capability"] = "changed locally"
                    self.assertEqual(context.kiro_consent["capability"], "fs_write")
                    if allow:
                        return PermissionResultAllow(option_id="always-yes", kiro_consent={
                            "scope": "workspace", "resource": "/tmp/test/a", "workspaceRoot": "/tmp/test"})
                    return PermissionResultDeny(option_id="always-no", kiro_consent={
                        "scope": "user", "resource": "/tmp/test/a"})
                options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", can_use_tool=decide)
                async with KiroSDKClient(options) as client:
                    await client.query("permission_consent")
                    result = [message async for message in client.receive_response()][-1]
                    response = json.loads(result.result)
                    self.assertEqual(response["outcome"]["optionId"], "always-yes" if allow else "always-no")
                    self.assertEqual(response["_meta"]["kiro"]["consent"]["scope"],
                                     "workspace" if allow else "user")
                    self.assertEqual(bool(result.permission_denials), not allow)

    async def test_invalid_consent_never_sends_persistent_decision(self):
        cases = [
            ("v2", {"scope": "session", "resource": "a"}, "always-yes", {}),
            ("v3", {"scope": "other", "resource": "a"}, "always-yes", {}),
            ("v3", {"scope": "workspace", "resource": "a", "workspaceRoot": "/other"}, "always-yes", {}),
            ("v3", {"scope": "workspace", "resource": "a"}, "always-yes", {}),
            ("v3", {"scope": "session", "resource": "a"}, "yes", {}),
            ("v3", {"scope": "session", "resource": "a"}, "always-yes", {"FAKE_CONSENT_DISABLED": "1"}),
        ]
        for engine, consent, option_id, env in cases:
            with self.subTest(engine=engine, consent=consent, option_id=option_id, env=env):
                async def decide(name, input_data, context):
                    return PermissionResultAllow(option_id=option_id, kiro_consent=consent)
                options = KiroAgentOptions(cli_path=FAKE, agent_engine=engine, env=env, can_use_tool=decide)
                async with KiroSDKClient(options) as client:
                    await client.query("permission_consent")
                    with self.assertRaisesRegex(ProtocolError, "permission callback or hook failed"):
                        _ = [message async for message in client.receive_response()]

    async def test_hook_block_cannot_be_combined_with_persistent_consent(self):
        async def block(event, tool_id, context):
            return {'block': "blocked"}

        async def decide(name, input_data, context):
            return PermissionResultAllow(option_id="always-yes",
                                         kiro_consent={"scope": "session", "resource": "a"})

        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", can_use_tool=decide,
                                   hooks={"preToolUse": [HookMatcher(hooks=[block])]})
        async with KiroSDKClient(options) as client:
            await client.query("permission_consent")
            with self.assertRaisesRegex(ProtocolError, "permission callback or hook failed") as raised:
                _ = [message async for message in client.receive_response()]
            self.assertIn("persistent consent was not applied", str(raised.exception.__cause__))


if __name__ == "__main__":
    unittest.main()
