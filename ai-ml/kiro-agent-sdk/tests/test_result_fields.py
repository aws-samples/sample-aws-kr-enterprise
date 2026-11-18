import copy
import pickle
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (InMemorySessionStore, KiroAgentOptions, KiroSDKClient,
                            PermissionResultAllow, ResultError, ResultMessage, project_key_for_directory)


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class ResultCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_result_error_exposes_payload_and_survives_copy(self):
        options = KiroAgentOptions(cli_path=FAKE, output_format={
            "type": "json_schema", "schema": {"type": "object"}})
        async with KiroSDKClient(options) as client:
            await client.query("not-json")
            with self.assertRaises(ResultError) as caught:
                _ = [message async for message in client.receive_response()]
        error = caught.exception
        self.assertEqual(error.kind, "invalid_structured_output")
        self.assertEqual(error.result, "not-json")
        self.assertEqual(error.session_id, "test-session")
        self.assertTrue(error.errors)
        for clone in (copy.copy(error), copy.deepcopy(error), pickle.loads(pickle.dumps(error))):
            self.assertEqual(clone.data, error.data)
            self.assertEqual(str(clone), str(error))

    async def test_observed_permission_denial_is_reported_and_mirrored(self):
        with tempfile.TemporaryDirectory() as root:
            store = InMemorySessionStore()
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, session_store=store)
            async with KiroSDKClient(options) as client:
                await client.query("permission")
                messages = [message async for message in client.receive_response()]
                result = next(message for message in messages if isinstance(message, ResultMessage))
                expected = [{"tool_name": "write", "tool_call_id": "call-1", "tool_input": {"path": "a"}}]
                self.assertEqual(result.permission_denials, expected)
                entries = await store.load({"project_key": project_key_for_directory(root),
                                            "session_id": client.session_id})
                mirrored = [entry for entry in entries if entry["type"] == "result"]
                self.assertEqual(mirrored[-1]["data"]["permission_denials"], expected)
                await client.query("ordinary")
                result = [message async for message in client.receive_response()][-1]
                self.assertIsNone(result.permission_denials)

    async def test_allow_and_missing_reject_option_are_not_reported_as_denials(self):
        async def allow(name, input_data, context):
            return PermissionResultAllow()

        for prompt, options in (
            ("permission", KiroAgentOptions(cli_path=FAKE, can_use_tool=allow)),
            ("permission_always", KiroAgentOptions(cli_path=FAKE)),
        ):
            with self.subTest(prompt=prompt):
                async with KiroSDKClient(options) as client:
                    await client.query(prompt)
                    result = [message async for message in client.receive_response()][-1]
                    self.assertIsNone(result.permission_denials)


if __name__ == "__main__":
    unittest.main()
