import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (CLIConnectionError, KiroAgentOptions, KiroSDKClient,
                            ProtocolError, create_mcp_server, tool)


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class McpConfigTests(unittest.IsolatedAsyncioTestCase):

    async def test_inline_json_and_reconnect_reload(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text('{"mcpServers":{"one":{"command":"first"}}}')
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, mcp_servers=str(path))
            client = KiroSDKClient(options)
            async with client:
                self.assertEqual(client.session_info["received"]["mcpServers"][0]["command"], "first")
            path.write_text('{"mcpServers":{"one":{"command":"second"}}}')
            async with client:
                self.assertEqual(client.session_info["received"]["mcpServers"][0]["command"], "second")
            options.mcp_servers = '{"mcpServers":{"inline":{"command":"third"}}}'
            async with client:
                self.assertEqual(client.session_info["received"]["mcpServers"][0]["command"], "third")

    async def test_omitted_mcp_wire_fields_are_sent_as_empty_lists(self):
        servers = {"local": {"command": "/bin/example"},
                   "remote": {"type": "http", "url": "https://example.test/mcp"}}
        options = KiroAgentOptions(cli_path=FAKE, mcp_servers=servers)
        async with KiroSDKClient(options) as client:
            sent = {server["name"]: server for server in client.session_info["received"]["mcpServers"]}
            self.assertEqual(sent["local"]["args"], [])
            self.assertEqual(sent["local"]["env"], [])
            self.assertEqual(sent["remote"]["headers"], [])
        self.assertNotIn("env", servers["local"])
        self.assertNotIn("headers", servers["remote"])

    async def test_invalid_programmatic_mcp_wire_fields_are_rejected(self):
        for server in ({"command": "x", "args": "invalid"}, {"command": "x", "env": {"A": 1}},
                       {"type": "http", "url": "https://example.test", "headers": [{"name": "X"}]}):
            with self.subTest(server=server):
                client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, mcp_servers={"bad": server}))
                with self.assertRaises(ValueError):
                    await client.connect()

    async def test_programmatic_expansion_is_opt_in_and_does_not_mutate_input(self):
        server = {"command": "${MCP_EXEC}", "args": ["${MCP_ARG:-default}"],
                  "env": [{"name": "TOKEN", "value": "${MCP_VALUE}"}]}
        with tempfile.TemporaryDirectory() as root:
            env = {"MCP_EXEC": "/bin/example", "MCP_VALUE": "token"}
            for mode in ("literal", "expand"):
                options = KiroAgentOptions(cwd=root, cli_path=FAKE, env=env, mcp_env_mode=mode,
                                           mcp_servers={"local": server})
                async with KiroSDKClient(options) as client:
                    sent = client.session_info["received"]["mcpServers"][0]
                    self.assertEqual(sent["command"], "${MCP_EXEC}" if mode == "literal" else "/bin/example")
                    self.assertEqual(sent["env"][0]["value"], "${MCP_VALUE}" if mode == "literal" else "token")
                    self.assertEqual(sent["args"][0], "${MCP_ARG:-default}" if mode == "literal" else "default")
            self.assertEqual(server["command"], "${MCP_EXEC}")
            self.assertEqual(server["env"][0]["value"], "${MCP_VALUE}")

    async def test_programmatic_expansion_rejects_missing_values(self):
        options = KiroAgentOptions(cli_path=FAKE, mcp_env_mode="expand",
                                   mcp_servers={"local": {"command": "${KIRO_TEST_MISSING_9543}"}})
        with self.assertRaisesRegex(ValueError, "not set"):
            await KiroSDKClient(options).connect()

    async def test_process_wide_mcp_toggle_is_a_notification_and_rejects_busy_turns(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3",
                                       env={"FAKE_LOG_REQUESTS": str(log)})
            async with KiroSDKClient(options) as client:
                await client.request_kiro_mcp_enabled(False)
                await client.request_kiro_mcp_enabled(True)
                with self.assertRaises(ValueError):
                    await client.request_kiro_mcp_enabled(1)
                # A later response is a fixture barrier: notifications have no
                # acknowledgement, so disconnecting immediately can race the log.
                await client.extension_request("_kiro/test", {})
                await client.query("cancel")
                with self.assertRaises(CLIConnectionError):
                    await client.request_kiro_mcp_enabled(False)
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            toggles = [item for item in requests if item["method"] == "_kiro/mcp/toggle"]
            self.assertEqual([item["params"]["enabled"] for item in toggles], [False, True])
            self.assertTrue(all("id" not in item for item in toggles))
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine="v2")) as client:
            with self.assertRaises(ProtocolError):
                await client.request_kiro_mcp_enabled(False)

    async def test_expansion_preserves_in_process_sdk_handlers(self):
        @tool("probe", "Test", {})
        async def probe(args):
            return {"content": []}
        server = create_mcp_server("sdk", tools=[probe])
        server["version"] = "${KIRO_TEST_MISSING_9543}"
        options = KiroAgentOptions(cli_path=FAKE, mcp_env_mode="expand", mcp_servers={"sdk": server})
        async with KiroSDKClient(options) as client:
            self.assertIs(client._configured_mcp_servers["sdk"]["tools"][0], probe)
            self.assertEqual(client._configured_mcp_servers["sdk"]["version"], "${KIRO_TEST_MISSING_9543}")

    async def test_malformed_and_unsupported_json_fails_before_start(self):
        configs = [
            '{"mcpServers":{"one":{"command":"x","command":"y"}}}',
            '{"mcpServers":{"one":{"command":"${KIRO_TEST_MISSING_12871}"}}}',
            '{"mcpServers":{"one":{"command":"${BAD!}"}}}',
            '{"mcpServers":{"one":{"type":"sdk"}}}',
            '{"mcpServers":{"one":{"type":"stdio","command":"x","disabled":true}}}',
            '{"mcpServers":{},"extra":true}',
            '{"mcpServers":{"one":{"command":NaN}}}',
        ]
        with tempfile.TemporaryDirectory() as root:
            for config in configs:
                with self.subTest(config=config):
                    client = KiroSDKClient(KiroAgentOptions(cwd=root, cli_path=FAKE, mcp_servers=config))
                    with self.assertRaises(ValueError):
                        await client.connect()
                    self.assertIsNone(client._process)


if __name__ == "__main__":
    unittest.main()
