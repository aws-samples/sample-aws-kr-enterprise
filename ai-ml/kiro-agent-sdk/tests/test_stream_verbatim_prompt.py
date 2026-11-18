import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (AgentDefinition, HookMatcher, KiroAgentOptions, KiroSDKClient, ResultMessage,
                            VerbatimPrompt, query)

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class StreamVerbatimPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_verbatim_item_without_runtime_is_rejected_before_prompt_or_hooks(self):
        called = []
        async def hook(*args):
            called.append(True)
            return {}
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            async def stream():
                yield VerbatimPrompt("/skill @/private/file")
            options = KiroAgentOptions(cli_path=FAKE, env={"FAKE_LOG_REQUESTS": str(log)},
                                       hooks={"promptSubmit": [HookMatcher(hooks=[hook])]})
            with self.assertRaises(ValueError) as error:
                _ = [event async for event in query(prompt=stream(), options=options)]
            self.assertNotIn("/private/file", str(error.exception))
            self.assertEqual(called, [])
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(item.get("method") == "session/prompt" for item in requests))

    async def test_plain_item_runs_and_later_verbatim_item_is_not_sent(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            async def stream():
                yield "first"
                yield VerbatimPrompt("must not execute")
            client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, env={"FAKE_LOG_REQUESTS": str(log)}))
            async with client:
                await client.query(stream())
                first = [event async for event in client.receive_response()]
                self.assertEqual(next(event.result for event in first if isinstance(event, ResultMessage)), "first")
                with self.assertRaisesRegex(ValueError, "VerbatimPrompt"):
                    _ = [event async for event in client.receive_response()]
                self.assertFalse(client._connected)
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            prompts = [item for item in requests if item.get("method") == "session/prompt"]
            self.assertEqual(len(prompts), 1)
            self.assertNotIn("must not execute", json.dumps(requests))

    async def test_initial_agent_turn_may_already_have_run(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            async def stream():
                yield VerbatimPrompt("must not execute")
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", agent="selected",
                env={"KIRO_HOME": str(Path(root) / "kiro"), "FAKE_LOG_REQUESTS": str(log)},
                agents={"selected": AgentDefinition("Test", "Test", initial_prompt="initial")})
            with self.assertRaisesRegex(ValueError, "VerbatimPrompt"):
                _ = [event async for event in query(prompt=stream(), options=options)]
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            prompts = [item for item in requests if item.get("method") == "session/prompt"]
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["params"]["prompt"][0]["text"], "initial")
            self.assertNotIn("must not execute", json.dumps(requests))


if __name__ == "__main__":
    unittest.main()
