"""Enabled MCP and context controls: returned values and closed-connection guards."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import (CLIConnectionError, KiroAgentOptions, KiroSDKClient, ProtocolError, create_mcp_server)

FAKE = str(Path(__file__).with_name("fake_kiro.py"))
CLOSED = (CLIConnectionError, ProtocolError)


async def authorize(url):
    return url


class McpControlLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "requests.jsonl"

    def requests(self):
        return self.log.read_bytes() if self.log.exists() else b""

    def sdk_client(self):
        return KiroSDKClient(KiroAgentOptions(
            cwd=self.root, cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-tools",
            mcp_servers={"server": create_mcp_server("server", tools=[])},
            env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log)}))

    async def test_sdk_server_toggle_and_cancellation_return_none_and_reject_closed_client(self):
        client = self.sdk_client()
        try:
            for phase in ("before", "connected", "after"):
                if phase == "connected":
                    await client.connect()
                elif phase == "after":
                    await client.disconnect()
                for operation in (lambda: client.toggle_mcp_server("server", False),
                                  lambda: client.toggle_mcp_server("server", True),
                                  lambda: client.request_mcp_tool_cancellation("server")):
                    with self.subTest(phase=phase):
                        before = self.requests()
                        if phase == "connected":
                            self.assertIsNone(await operation())
                        else:
                            with self.assertRaises(CLIConnectionError):
                                await operation()
                        self.assertEqual(self.requests(), before)
                for name, args in (("toggle_mcp_server", ("", True)), ("toggle_mcp_server", ("server", "yes")),
                                   ("request_mcp_tool_cancellation", ("",))):
                    with self.subTest(phase=phase, invalid=name), self.assertRaises(ValueError):
                        await getattr(client, name)(*args)
        finally:
            await client.disconnect()


@unittest.skipUnless(os.name == "posix", "requires local POSIX Kiro fixture")
class RootsNotificationContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / "node_modules/@kiro/agent/dist/server/acp-server.js"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"fixture")
        source.with_name("xhr-sync-worker.js").write_bytes(b"worker")
        patcher = patch("kiro_agent_sdk.native_hook_runtime.patch_runtime", return_value=b"patched fixture")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = self.root / "notifications.jsonl"
        self.options = KiroAgentOptions(
            cwd=self.root, cli_path=FAKE, agent_engine="v3", tools=[],
            runtime_compat_mode="v3", native_hook_runtime_path=source,
            mcp_elicitation_hook_mode="sdk", mcp_elicitation_runtime_mode="v3",
            mcp_server_request_allowlist=["policy"], mcp_server_request_scope="connection",
            mcp_roots_changed_mode="runtime", mcp_server_request_authorizer=AsyncMock(return_value=True),
            on_mcp_roots=AsyncMock(return_value={"roots": []}),
            env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_RUNTIME_POLICIES": "1",
                 "FAKE_ROOTS_NOTIFICATION_LOG": str(self.log)})

    def actions(self):
        return [json.loads(line)["action"] for line in self.log.read_text().splitlines()] if self.log.exists() else []


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "requires POSIX no-follow reads")
class ContextControlLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "requests.jsonl"
        self.env = {"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log)}

    def prompts(self):
        return [r["params"]["prompt"] for r in map(json.loads, self.log.read_text().splitlines())
                if r.get("method") == "session/prompt"] if self.log.exists() else []

    async def turn(self, client, text):
        await client.query(text)
        return [message async for message in client.receive_response()][-1].result


if __name__ == "__main__":
    unittest.main()
