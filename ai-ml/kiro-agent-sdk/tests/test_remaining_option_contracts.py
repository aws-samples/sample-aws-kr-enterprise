"""Change semantics of mapped permissions, MCP OAuth and external hook settings, and the SubagentStop limit scope."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, McpOAuthConfig, PermissionResultAllow
from test_mcp_oauth import OAuthFixture

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


async def allow(name, data, context):
    return PermissionResultAllow()


async def match(rule, context):
    return True


async def other_match(rule, context):
    return True


@unittest.skipIf(importlib.util.find_spec("mcp") is None, "requires the optional MCP transport")
class McpOAuthChangeContracts(unittest.IsolatedAsyncioTestCase):
    async def test_oauth_configuration_is_kept_for_the_session_and_reread_on_reconnect(self):
        fixture = OAuthFixture()
        url = await fixture.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                options = KiroAgentOptions(
                    cwd=root, env={"KIRO_HOME": str(Path(root) / "kiro")}, cli_path=FAKE, agent_engine="v3",
                    mcp_servers={"probe": {"type": "http", "url": url}}, sdk_mcp_toggle_mode="sdk-all",
                    mcp_oauth={"probe": McpOAuthConfig("http://127.0.0.1:9999/callback", fixture.authorize)})
                client = KiroSDKClient(options)
                try:
                    await client.connect()
                    oauth = client._named_external_mcp_bridges["probe"][0].oauth
                    self.assertIsNotNone(oauth)
                    options.mcp_oauth = {}
                    await client.query("hello")
                    self.assertEqual([m async for m in client.receive_response()][-1].result, "hello")
                    self.assertIs(client._named_external_mcp_bridges["probe"][0].oauth, oauth)
                    await client.disconnect()
                    await client.connect()
                    self.assertIsNone(client._named_external_mcp_bridges["probe"][0].oauth)
                finally:
                    await client.disconnect()
        finally:
            await fixture.close()


if __name__ == "__main__":
    unittest.main()
