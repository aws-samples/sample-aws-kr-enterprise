"""Read-only diagnostic properties remain observable across client lifetimes."""
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient
from test_transport import FakeTransport


EMPTY_DIAGNOSTICS = {
    "last_mirror_error": None,
    "sdk_permission_mode": "ask",
    "reported_credits": 0.0,
    "last_rewind_fork_id": None,
    "restored_session_id": None,
}


class PublicPropertyContractTests(unittest.IsolatedAsyncioTestCase):

    async def test_callers_cannot_assign_read_only_diagnostics_or_change_permission_state(self):
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=FakeTransport())
        async with client:
            for name, expected in EMPTY_DIAGNOSTICS.items():
                with self.subTest(property=name):
                    with self.assertRaises(AttributeError):
                        setattr(client, name, "forged")
                    self.assertEqual(getattr(client, name), expected)
            self.assertEqual(client.sdk_permission_mode, "ask")


if __name__ == "__main__":
    unittest.main()
