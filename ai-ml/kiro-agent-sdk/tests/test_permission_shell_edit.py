import json
import unittest
from pathlib import Path

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, PermissionResultAllow, ProtocolError


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class PermissionShellEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_input_is_a_noop(self):
        async def allow(name, input_data, context):
            return PermissionResultAllow(updated_input=dict(input_data))
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, can_use_tool=allow)) as client:
            await client.query("permission")
            result = [message async for message in client.receive_response()][-1]
            self.assertEqual(result.result, "yes")

    async def test_explicit_shell_edit_sends_kiro_metadata(self):
        async def edit(name, input_data, context):
            return PermissionResultAllow(updated_input={**input_data, "command": "printf AFTER"})
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", kiro_shell_edit=True, can_use_tool=edit)
        async with KiroSDKClient(options) as client:
            await client.query("permission_shell_edit")
            result = [message async for message in client.receive_response()][-1]
            response = json.loads(result.result)
            self.assertEqual(response["outcome"]["optionId"], "yes")
            self.assertEqual(response["_meta"]["kiro"]["editedCommand"], "printf AFTER")

    async def test_empty_shell_input_can_be_edited_from_consent_context(self):
        async def edit(name, input_data, context):
            self.assertEqual(input_data, {})
            self.assertEqual(context.kiro_consent["capability"], "shell")
            return PermissionResultAllow(updated_input={"command": "printf AFTER"})
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", kiro_shell_edit=True, can_use_tool=edit)
        async with KiroSDKClient(options) as client:
            await client.query("permission_shell_sparse")
            result = [message async for message in client.receive_response()][-1]
            self.assertEqual(json.loads(result.result)["_meta"]["kiro"]["editedCommand"], "printf AFTER")

    async def test_unsupported_edits_fail_closed(self):
        cases = [
            (False, "permission_shell_edit", {"command": "printf AFTER", "timeout": 5}, None),
            (True, "permission_consent", {"path": "different"}, None),
            (True, "permission_shell_edit", {"command": "printf AFTER", "timeout": 10}, None),
            (True, "permission_shell_edit", {"command": "printf AFTER"}, None),
            (True, "permission_shell_edit", {"command": " ", "timeout": 5}, None),
            (True, "permission_shell_edit", {"command": "printf AFTER", "timeout": 5}, "always-yes"),
        ]
        for enabled, prompt, updated, option_id in cases:
            with self.subTest(enabled=enabled, prompt=prompt, updated=updated, option_id=option_id):
                async def edit(name, input_data, context):
                    return PermissionResultAllow(updated_input=updated, option_id=option_id)
                options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", kiro_shell_edit=enabled, can_use_tool=edit)
                async with KiroSDKClient(options) as client:
                    await client.query(prompt)
                    with self.assertRaisesRegex(ProtocolError, "permission callback or hook failed"):
                        _ = [message async for message in client.receive_response()]

    async def test_hook_denial_cannot_execute_edit(self):
        async def deny(event, tool_id, context):
            return {'block': "blocked"}
        async def edit(name, input_data, context):
            return PermissionResultAllow(updated_input={**input_data, "command": "printf AFTER"})
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", kiro_shell_edit=True,
                                   can_use_tool=edit, hooks={"preToolUse": [HookMatcher(hooks=[deny])]})
        async with KiroSDKClient(options) as client:
            await client.query("permission_shell_edit")
            result = [message async for message in client.receive_response()][-1]
            response = json.loads(result.result)
            self.assertEqual(response["outcome"]["optionId"], "no")
            self.assertNotIn("editedCommand", response.get("_meta", {}).get("kiro", {}))

    async def test_conflicting_hook_edits_and_callbacks_fail_closed(self):
        async def first(event, tool_id, context):
            return {'allow': True, 'tool_input': {**event["tool_input"], "command": "printf FIRST"}}
        async def second(event, tool_id, context):
            return {'allow': True, 'tool_input': {**event["tool_input"], "command": "printf SECOND"}}
        async def allow(name, input_data, context):
            return PermissionResultAllow()
        cases = [([first, second], None), ([first], allow)]
        for hooks, callback in cases:
            with self.subTest(hooks=len(hooks), callback=bool(callback)):
                options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", kiro_shell_edit=True,
                    can_use_tool=callback, hooks={"preToolUse": [HookMatcher(hooks=hooks)]})
                async with KiroSDKClient(options) as client:
                    await client.query("permission_shell_edit")
                    with self.assertRaisesRegex(ProtocolError, "permission callback or hook failed"):
                        _ = [message async for message in client.receive_response()]

    async def test_hook_mutation_does_not_change_other_hooks_or_callback_input(self):
        seen = []
        async def mutate(event, tool_id, context):
            event["tool_input"]["path"] = "forged"
            return {}
        async def observe(event, tool_id, context):
            seen.append(event["tool_input"]["path"])
            return {}
        async def allow(name, input_data, context):
            seen.append(input_data["path"])
            context.tool_call["rawInput"]["path"] = "also-forged"
            return PermissionResultAllow(updated_input=input_data)
        options = KiroAgentOptions(cli_path=FAKE, can_use_tool=allow,
                                   hooks={"preToolUse": [HookMatcher(hooks=[mutate, observe])]})
        async with KiroSDKClient(options) as client:
            await client.query("permission")
            result = [message async for message in client.receive_response()][-1]
            self.assertEqual(result.result, "yes")
        self.assertEqual(seen, ["a", "a"])

    async def test_callback_cannot_silently_mutate_input_or_permission_options(self):
        async def mutate_input(name, input_data, context):
            input_data["path"] = "different"
            return PermissionResultAllow(updated_input=input_data)
        options = KiroAgentOptions(cli_path=FAKE, can_use_tool=mutate_input)
        async with KiroSDKClient(options) as client:
            await client.query("permission")
            with self.assertRaisesRegex(ProtocolError, "permission callback or hook failed"):
                _ = [message async for message in client.receive_response()]

        async def mutate_options(name, input_data, context):
            context.options[0]["kind"] = "allow_once"
            return PermissionResultAllow()
        options = KiroAgentOptions(cli_path=FAKE, can_use_tool=mutate_options)
        async with KiroSDKClient(options) as client:
            await client.query("permission_always")
            result = [message async for message in client.receive_response()][-1]
            self.assertEqual(result.result, "cancelled")

    async def test_requires_tested_cli_version_and_cannot_be_enabled_after_connect(self):
        async def edit(name, input_data, context):
            return PermissionResultAllow(updated_input={**input_data, "command": "printf AFTER"})
        unsupported = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", kiro_shell_edit=True,
                                       can_use_tool=edit, env={"FAKE_VERSION": "kiro-cli 2.34.0"})
        client = KiroSDKClient(unsupported)
        with self.assertRaisesRegex(ProtocolError, "requires the tested Kiro CLI"):
            await client.connect()
        self.assertIsNone(client._process)
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", can_use_tool=edit)
        async with KiroSDKClient(options) as client:
            options.kiro_shell_edit = True
            await client.query("permission_shell_edit")
            with self.assertRaisesRegex(ProtocolError, "permission callback or hook failed"):
                _ = [message async for message in client.receive_response()]


if __name__ == "__main__":
    unittest.main()
