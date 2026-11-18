"""Cached, side-effect-free client views and no-op interruption lifetimes."""
import unittest

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient
from test_transport import FakeTransport


ASYNC_EMPTY = ["get_context_usage", "get_context_usage_raw", "get_mcp_status",
               "get_mcp_status_raw", "get_sandbox_status"]
SYNC_EMPTY = {
    "get_portable_skill_tools": {},
    "get_portable_skill_catalog_tools": {},
    "get_portable_plugin_skill_catalog_tools": {},
    "get_skill_fork_sessions": [],
    "get_model_hook_sessions": [],
}


class PublicCacheContractTests(unittest.IsolatedAsyncioTestCase):

    async def test_interrupt_without_a_session_is_a_noop_and_does_not_prevent_connection(self):
        peer = FakeTransport()
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=peer)
        self.assertIsNone(await client.interrupt())
        self.assertEqual(peer.requests, [])
        await client.connect()
        await client.disconnect()
        before = len(peer.requests)
        self.assertIsNone(await client.interrupt())
        self.assertEqual(len(peer.requests), before)


if __name__ == "__main__":
    unittest.main()
