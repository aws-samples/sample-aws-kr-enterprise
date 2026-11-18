import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import AgentDefinition, KiroAgentOptions, KiroSDKClient


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class UnsupportedLimitTests(unittest.IsolatedAsyncioTestCase):

    async def test_mutated_limits_fail_before_next_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, env={"FAKE_LOG_REQUESTS": str(log)})
            async with KiroSDKClient(options) as client:
                for field, invalid, default in (
                    ("strict_mcp_config", True, False), ("max_turns", 1, None),
                    ("fallback_model", "other", None),
                ):
                    with self.subTest(field=field):
                        setattr(options, field, invalid)
                        with self.assertRaises(ValueError):
                            await client.query("must-not-run")
                        setattr(options, field, default)
                await client.query("ok")
                self.assertEqual([message async for message in client.receive_response()][-1].result, "ok")
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            prompts = [request["params"]["prompt"][0]["text"] for request in requests
                       if request.get("method") == "session/prompt"]
            self.assertEqual(prompts, ["ok"])

    async def test_agent_limits_accept_only_unset_values(self):
        with tempfile.TemporaryDirectory() as root:
            env = {"KIRO_HOME": str(Path(root) / "kiro")}
            definition = AgentDefinition("Test", "Test", max_turns=None)
            async with KiroSDKClient(KiroAgentOptions(cwd=root, cli_path=FAKE, env=env,
                                                     agents={"worker": definition})):
                pass
            for changes in ({"max_turns": 0}, {"max_turns": 2}):
                with self.subTest(changes=changes):
                    definition = AgentDefinition("Test", "Test", **changes)
                    client = KiroSDKClient(KiroAgentOptions(cwd=root, cli_path=FAKE, env=env,
                                                            agents={"worker": definition}))
                    with self.assertRaises(ValueError):
                        await client.connect()
                    self.assertFalse((Path(root) / "kiro" / "agents" / "worker.json").exists())


if __name__ == "__main__":
    unittest.main()
