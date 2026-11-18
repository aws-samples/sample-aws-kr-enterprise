"""Return values and caller-option preservation for direct SDK mode controls."""
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient
from test_transport import FakeTransport


class PublicModeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_mode_and_permission_setters_preserve_caller_options_and_return_contracts(self):
        options = KiroAgentOptions(agent_engine="v3", model="model-one", permission_mode="ask")
        peer = FakeTransport()
        async with KiroSDKClient(options, transport=peer) as client:
            self.assertIsNone(await client.set_model("model-two"))
            self.assertEqual(client._session_model, "model-two")
            self.assertEqual(options.model, "model-one")
            self.assertIsNone(await client.set_mode("vibe"))
            self.assertEqual(client._current_mode, "vibe")
            configured = await client.set_config_option("effortLevel", "low")
            self.assertEqual(configured["configOptions"], [{"id": "effortLevel", "currentValue": "low"}])
            self.assertIsNone(await client.set_permission_mode("denyUnlisted"))
            self.assertEqual(client.sdk_permission_mode, "denyUnlisted")
            self.assertEqual(options.permission_mode, "ask")
            self.assertEqual([row["params"] for row in peer.requests if row["method"] == "session/set_mode"],
                             [{"sessionId": "remote", "modeId": "vibe"}])


if __name__ == "__main__":
    unittest.main()
