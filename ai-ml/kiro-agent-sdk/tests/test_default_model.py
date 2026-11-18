import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport


FAKE = Path(__file__).with_name("fake_kiro.py").resolve()


class DefaultModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_uses_local_cli_model_for_both_engines(self):
        with tempfile.TemporaryDirectory() as root:
            for engine in ("v2", "v3"):
                with self.subTest(engine=engine):
                    log = Path(root) / (engine + ".jsonl")
                    options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine=engine,
                                               env={"FAKE_DEFAULT_MODEL": "example",
                                                    "FAKE_LOG_REQUESTS": str(log)})
                    async with KiroSDKClient(options) as client:
                        await client.set_model()
                        self.assertIsNone(client.options.model)
                        self.assertEqual(client._session_model, "example")
                    requests = [json.loads(line) for line in log.read_text().splitlines()]
                    method = "session/set_config_option" if engine == "v3" else "session/set_model"
                    selection = next(item for item in requests if item["method"] == method)
                    self.assertEqual(selection["params"].get("value" if engine == "v3" else "modelId"),
                                     "example")

    async def test_missing_invalid_or_unlisted_default_is_not_selected(self):
        for env in ({}, {"FAKE_DEFAULT_MODEL": ""}, {"FAKE_DEFAULT_MODEL": "missing",
                                                       "FAKE_UNLISTED_DEFAULT": "1"}):
            with self.subTest(env=env):
                with tempfile.TemporaryDirectory() as root:
                    log = Path(root) / "requests.jsonl"
                    options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                                               env={**env, "FAKE_LOG_REQUESTS": str(log)})
                    async with KiroSDKClient(options) as client:
                        with self.assertRaisesRegex(ProtocolError, "default model"):
                            await client.set_model(None)
                    requests = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertFalse(any(item["method"] == "session/set_config_option" for item in requests))

    async def test_remote_requires_explicit_model_and_disconnected_client_fails(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE))
        with self.assertRaises(CLIConnectionError):
            await client.set_model()
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            with self.assertRaisesRegex(ProtocolError, "explicit model"):
                await client.set_model()
            await client.set_model("remote-model")
        self.assertFalse(any(item.get("params", {}).get("value") is None
                             for item in transport.requests if item["method"] == "session/set_config_option"))

    async def test_model_list_timeout_does_not_change_session_model(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", env={"FAKE_LIST_MODELS_HANG": "1"})
        async with KiroSDKClient(options) as client:
            original = client._session_model
            options.startup_timeout = 0.05
            with self.assertRaisesRegex(CLIConnectionError, "Timed out"):
                await client.set_model()
            self.assertEqual(client._session_model, original)


if __name__ == "__main__":
    unittest.main()
