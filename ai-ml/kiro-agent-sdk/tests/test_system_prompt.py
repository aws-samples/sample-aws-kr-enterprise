import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class SystemPromptTests(unittest.IsolatedAsyncioTestCase):

    async def test_unsupported_forms_fail_before_start(self):
        invalid = [
            {"type": "custom", "prompt": "x", "snapshot": True},
            {"type": "custom", "prompt": "x", "snapshot": False},
            {"type": "custom", "prompt": "x", "snapshot": 0},
            {"type": "custom", "prompt": "file:///not-literal"},
            {"type": "custom", "prompt": 1},
            {"type": "file", "path": 1},
            {"type": "preset", "preset": "default"},
        ]
        with tempfile.TemporaryDirectory() as root:
            env = {"KIRO_HOME": str(Path(root) / "kiro")}
            for setting in invalid:
                with self.subTest(setting=setting):
                    client = KiroSDKClient(KiroAgentOptions(cwd=root, cli_path=FAKE, env=env,
                                                            system_prompt=setting))
                    with self.assertRaises(ValueError):
                        await client.connect()
                    self.assertIsNone(client._process)
                    self.assertFalse((Path(root) / "kiro" / "agents").exists())
            path = Path(root) / "prompt.txt"
            path.write_text("file:///not-literal")
            client = KiroSDKClient(KiroAgentOptions(cwd=root, cli_path=FAKE, env=env,
                                                    system_prompt={"type": "file", "path": path}))
            with self.assertRaises(ValueError):
                await client.connect()


if __name__ == "__main__":
    unittest.main()
