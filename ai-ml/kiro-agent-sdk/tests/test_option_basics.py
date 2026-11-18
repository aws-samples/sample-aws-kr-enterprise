import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (AgentDefinition, KiroAgentOptions, KiroSDKClient,
                            ToolPermissionContext)


FAKE = Path(__file__).with_name("fake_kiro.py").resolve()


class OptionCompatibilityTests(unittest.IsolatedAsyncioTestCase):

    async def test_permission_context_supports_default_construction(self):
        first = ToolPermissionContext()
        second = ToolPermissionContext()
        first.options.append({"optionId": "local"})
        self.assertEqual(second.options, [])
        self.assertIsNone(first.kiro_consent)

    async def test_invalid_agents_are_rejected(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agents=[]))
        with self.assertRaisesRegex(TypeError, "agents"):
            await client.connect()
        with tempfile.TemporaryDirectory() as root:
            old_positional = AgentDefinition("Test", "Test", None, "model-id")
            client = KiroSDKClient(KiroAgentOptions(cwd=root, cli_path=FAKE,
                env={"KIRO_HOME": str(Path(root) / "kiro")}, agents={"worker": old_positional}))
            with self.assertRaisesRegex(ValueError, "disallowed_tools"):
                await client.connect()


if __name__ == "__main__":
    unittest.main()
