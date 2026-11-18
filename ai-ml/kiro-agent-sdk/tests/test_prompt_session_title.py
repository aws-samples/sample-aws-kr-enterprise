import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (HookMatcher, InMemorySessionStore, KiroAgentOptions, KiroSDKClient,
                            ProtocolError, list_sessions_from_store)


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class PromptSessionTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_title_is_set_before_prompt_and_mirrored(self):
        async def first(event, tool_id, context):
            return {'title': "first"}
        async def second(event, tool_id, context):
            return {'context': "extra", 'title': "  final title  "}
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            store = InMemorySessionStore()
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3", session_store=store,
                env={"FAKE_LOG_REQUESTS": str(log)},
                hooks={"promptSubmit": [HookMatcher(hooks=[first, second])]})
            async with KiroSDKClient(options) as client:
                await client.query("hello")
                _ = [message async for message in client.receive_response()]
                info = await list_sessions_from_store(store, directory=root)
                self.assertEqual(info[0].title, "final title")
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            actions = [request for request in requests if request.get("method") in
                       ("_kiro/session/rename", "session/prompt")]
            self.assertEqual([action["method"] for action in actions], ["_kiro/session/rename", "session/prompt"])
            self.assertEqual(actions[0]["params"]["title"], "final title")
            self.assertEqual(actions[1]["params"]["prompt"][1]["text"], "extra")

    async def test_blocked_and_failed_title_do_not_send_prompt(self):
        async def titled(event, tool_id, context):
            return {'title': "test"}
        async def blocked(event, tool_id, context):
            return {'block': True}
        with tempfile.TemporaryDirectory() as root:
            for engine, fail, hooks in (
                ("v3", False, [titled, blocked]),
                ("v3", True, [titled]),
                ("v2", False, [titled]),
            ):
                with self.subTest(engine=engine, fail=fail, hooks=len(hooks)):
                    log = Path(root) / "requests.jsonl"
                    if log.exists():
                        log.unlink()
                    env = {"FAKE_LOG_REQUESTS": str(log)}
                    if fail:
                        env["FAKE_RENAME_FAIL"] = "1"
                    options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine=engine, env=env,
                                               hooks={"promptSubmit": [HookMatcher(hooks=hooks)]})
                    async with KiroSDKClient(options) as client:
                        with self.assertRaises(ProtocolError):
                            await client.query("must-not-run")
                    requests = [json.loads(line) for line in log.read_text().splitlines()]
                    methods = [request.get("method") for request in requests]
                    self.assertNotIn("session/prompt", methods)
                    if len(hooks) == 2 or engine == "v2":
                        self.assertNotIn("_kiro/session/rename", methods)


if __name__ == "__main__":
    unittest.main()
