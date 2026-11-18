import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class PolicyPresetTests(unittest.IsolatedAsyncioTestCase):
    async def test_presets_are_sent_verified_and_reapplied_on_load_and_fork(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            env = {"FAKE_LOG_REQUESTS": str(log)}
            for changes in ({}, {"resume": "old"}, {"resume": "old", "fork_session": True}):
                options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3", env=env,
                                           kiro_policy_presets=["read-workspace", "read-workspace"], **changes)
                async with KiroSDKClient(options) as client:
                    rules = (await client.list_kiro_permissions())["rules"]
                    self.assertTrue(any(rule["source"] == "preset:read-workspace" for rule in rules))
                    with self.assertRaisesRegex(ProtocolError, "Plan mode"):
                        await client.set_permission_mode("plan")
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            sessions = [request for request in requests
                        if request.get("method") in ("session/new", "session/load", "session/fork")]
            self.assertEqual([request["method"] for request in sessions],
                             ["session/new", "session/load", "session/fork", "session/load"])
            for request in sessions:
                self.assertEqual(request["params"]["_meta"]["kiro"]["policyPreset"], ["read-workspace"])

    async def test_missing_reported_preset_fails_closed(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", env={"FAKE_IGNORE_PRESETS": "1"},
                                   kiro_policy_presets=["read-workspace"])
        client = KiroSDKClient(options)
        with self.assertRaisesRegex(ProtocolError, "did not report every requested"):
            await client.connect()
        self.assertFalse(client._connected)

    async def test_unknown_or_changed_plan_mode_fails_closed(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", env={"FAKE_NO_MODE": "1"},
                                   kiro_policy_presets=["read-workspace"])
        with self.assertRaisesRegex(ProtocolError, "non-Plan mode"):
            await KiroSDKClient(options).connect()
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", kiro_policy_presets=["read-workspace"])
        async with KiroSDKClient(options) as client:
            process = client._process
            await client.query("guard_preset_plan")
            await client._send({"jsonrpc": "2.0", "method": "_test/trigger", "params": {}})
            await asyncio.wait_for(process.wait(), 5)
            with self.assertRaisesRegex(ProtocolError, "Plan mode"):
                _ = [message async for message in client.receive_response()]

    async def test_rejects_unsafe_combinations_and_bad_ids(self):
        async def callback(*args):
            return None
        invalid = [
            {"agent_engine": "v2", "kiro_policy_presets": ["read-workspace"]},
            {"agent_engine": "v3", "kiro_policy_presets": ["bad id"]},
            {"agent_engine": "v3", "kiro_policy_presets": ["read-workspace"], "permission_mode": "plan"},
            {"agent_engine": "v3", "kiro_policy_presets": ["read-workspace"], "can_use_tool": callback},
            {"agent_engine": "v3", "kiro_policy_presets": ["read-workspace"],
             "hooks": {"preToolUse": [HookMatcher()]}},
        ]
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    KiroSDKClient(KiroAgentOptions(cli_path=FAKE, **values))


if __name__ == "__main__":
    unittest.main()
