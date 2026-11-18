import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (AgentMessage, CLIConnectionError, KiroAgentOptions, KiroSDKClient,
                            ProtocolError)


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class StartupFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_or_fallback_selected_from_reported_choices(self):
        for primary, expected in (("model-one", "model-one"), ("not-available", "model-two")):
            with self.subTest(primary=primary):
                options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", model=primary,
                    fallback_model="model-two", fallback_model_mode="startup", env={"FAKE_MODEL_OPTIONS": "1"})
                async with KiroSDKClient(options) as client:
                    self.assertEqual(client._session_model, expected)
                    self.assertEqual(options.model, primary)
                    self.assertNotIn("modelId", client.session_info["received"].get("_meta", {}).get("kiro", {}))
                    await client.query("hello")
                    messages = [message async for message in client.receive_response()]
                    self.assertEqual(next(message.model for message in messages
                                          if isinstance(message, AgentMessage)), expected)
                    await client.set_model("model-one")
                    await client.query("again")
                    self.assertEqual([message async for message in client.receive_response()][-1].result, "again")

    async def test_missing_or_unreported_models_fail_before_prompt(self):
        for env, fallback in (({}, "model-two"), ({"FAKE_MODEL_OPTIONS": "1"}, "also-missing")):
            with self.subTest(env=env):
                options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", model="missing",
                                           fallback_model=fallback, fallback_model_mode="startup", env=env)
                client = KiroSDKClient(options)
                with self.assertRaises(ProtocolError):
                    await client.connect()
                self.assertFalse(client._connected)

    async def test_rejected_selection_falls_back_before_any_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3", model="model-one",
                fallback_model="model-two", fallback_model_mode="startup",
                env={"FAKE_MODEL_OPTIONS": "1", "FAKE_MODEL_SET_FAIL_FOR": "model-one",
                     "FAKE_LOG_REQUESTS": str(log)})
            async with KiroSDKClient(options) as client:
                self.assertEqual(client._session_model, "model-two")
                await client.query("hello")
                messages = [message async for message in client.receive_response()]
                self.assertEqual(next(message.model for message in messages
                                      if isinstance(message, AgentMessage)), "model-two")
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            relevant = [(request["method"], request["params"].get("value")) for request in requests
                        if request.get("method") in ("session/set_config_option", "session/prompt")]
            self.assertEqual(relevant, [("session/set_config_option", "model-one"),
                                        ("session/set_config_option", "model-two"),
                                        ("session/prompt", None)])

    async def test_both_selection_failures_stop_without_a_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3", model="model-one",
                fallback_model="model-two", fallback_model_mode="startup",
                env={"FAKE_MODEL_OPTIONS": "1", "FAKE_MODEL_SET_FAIL": "1", "FAKE_LOG_REQUESTS": str(log)})
            with self.assertRaises(ProtocolError):
                await KiroSDKClient(options).connect()
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            selections = [request["params"]["value"] for request in requests
                          if request.get("method") == "session/set_config_option"
                          and request["params"].get("configId") == "model"]
            self.assertEqual(selections, ["model-one", "model-two"])
            self.assertFalse(any(request.get("method") == "session/prompt" for request in requests))

    async def test_uncertain_selection_timeout_does_not_retry(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3", model="model-one",
                fallback_model="model-two", fallback_model_mode="startup", startup_timeout=0.25,
                env={"FAKE_MODEL_OPTIONS": "1", "FAKE_MODEL_SET_HANG_FOR": "model-one",
                     "FAKE_LOG_REQUESTS": str(log)})
            with self.assertRaises(CLIConnectionError):
                await KiroSDKClient(options).connect()
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            selections = [request["params"]["value"] for request in requests
                          if request.get("method") == "session/set_config_option"]
            self.assertEqual(selections, ["model-one"])
            self.assertFalse(any(request.get("method") == "session/prompt" for request in requests))

    async def test_stream_cannot_change_fallback_between_turns(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", model="model-one",
            fallback_model="model-two", fallback_model_mode="startup", env={"FAKE_MODEL_OPTIONS": "1"})

        async def prompts():
            yield "first"
            options.fallback_model = "other"
            yield "must-not-run"

        async with KiroSDKClient(options) as client:
            await client.query(prompts())
            seen = []
            with self.assertRaisesRegex(ValueError, "Reconnect"):
                async for message in client.receive_messages():
                    seen.append(message)
            self.assertTrue(any(getattr(message, "result", None) == "first" for message in seen))


if __name__ == "__main__":
    unittest.main()
