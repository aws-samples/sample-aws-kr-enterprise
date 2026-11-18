"""Code intelligence controls and guarded fork lookup outside a healthy idle session."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient, ProtocolError

FAKE = str(Path(__file__).with_name("fake_kiro.py"))
CLOSED = (CLIConnectionError, ProtocolError)
CODE = ("get_kiro_code_intelligence_status", "initialize_kiro_code_intelligence", "get_kiro_code_overview")


class FixtureMixin:
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"

    def requests(self, method):
        if not self.log.exists():
            return []
        return [row for row in map(json.loads, self.log.read_text().splitlines()) if row.get("method") == method]


class CodeIntelligenceContractTests(FixtureMixin, unittest.IsolatedAsyncioTestCase):
    def options(self, enabled=True):
        return KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3",
                                enable_kiro_code_intelligence=enabled,
                                env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log)})

    async def test_each_control_rejects_closed_disabled_and_changed_sessions_without_dispatch(self):
        client = KiroSDKClient(self.options())
        try:
            for name in CODE:
                with self.subTest(phase="before", method=name), self.assertRaises(CLIConnectionError):
                    await getattr(client, name)()
            await client.connect()
            dispatched = len(self.requests("_kiro/codeIntelligence"))
            client.options.enable_kiro_code_intelligence = False
            for name in CODE:
                with self.subTest(phase="changed", method=name), self.assertRaisesRegex(ValueError, "Reconnect"):
                    await getattr(client, name)()
            client.options.enable_kiro_code_intelligence = True
            self.assertEqual(len(self.requests("_kiro/codeIntelligence")), dispatched)
            self.assertEqual((await client.get_kiro_code_overview())["subcommand"], "overview")
            await client.disconnect()
            dispatched = len(self.requests("_kiro/codeIntelligence"))
            for name in CODE:
                with self.subTest(phase="after", method=name), self.assertRaises(CLOSED):
                    await getattr(client, name)()
            self.assertEqual(len(self.requests("_kiro/codeIntelligence")), dispatched)
        finally:
            await client.disconnect()
        async with KiroSDKClient(self.options(enabled=False)) as disabled:
            for name in CODE:
                with self.subTest(phase="disabled", method=name), self.assertRaisesRegex(ProtocolError, "Enable"):
                    await getattr(disabled, name)()

    async def test_overview_rejects_an_unconsumed_turn(self):
        async with KiroSDKClient(self.options()) as client:
            await client.query("hello")
            with self.assertRaisesRegex(CLIConnectionError, "Consume"):
                await client.get_kiro_code_overview()
            _ = [message async for message in client.receive_response()]
            self.assertTrue((await client.get_kiro_code_overview())["success"])


class GuardedForkContractTests(FixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        source = self.root / "node_modules/@kiro/agent/dist/server/acp-server.js"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"fixture")
        source.with_name("xhr-sync-worker.js").write_bytes(b"worker")
        mock = patch("kiro_agent_sdk.native_hook_runtime.patch_runtime", return_value=b"patched fixture")
        mock.start()
        self.addCleanup(mock.stop)
        self.options = KiroAgentOptions(
            cwd=self.root, cli_path=FAKE, agent_engine="v3", runtime_compat_mode="v3",
            native_hook_runtime_path=source, tools=[],
            env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_RUNTIME_POLICIES": "1",
                 "FAKE_LOG_REQUESTS": str(self.log)})

    async def test_lookup_requires_live_runtime_valid_ids_and_sends_exact_request(self):
        client = KiroSDKClient(self.options)
        try:
            with self.assertRaisesRegex(CLIConnectionError, "connect"):
                await client.get_guarded_fork_options("user")
            await client.connect()
            for value in ("", None, "x" * 129, "a\nb"):
                with self.subTest(user_message_id=value), self.assertRaises(ValueError):
                    await client.get_guarded_fork_options(value)
            self.assertEqual(self.requests("_kiro/sdk/forkPoint"), [])
            self.assertEqual(await client.get_guarded_fork_options("user"), {
                "resume": "test-session", "fork_session": True,
                "resume_session_at": "prior-boundary", "resume_drops_turn": "user"})
            self.assertEqual([row["params"] for row in self.requests("_kiro/sdk/forkPoint")],
                             [{"sessionId": "test-session", "userMessageId": "user"}])
            await client.disconnect()
            with self.assertRaisesRegex(CLIConnectionError, "connect"):
                await client.get_guarded_fork_options("user")
            self.assertEqual(len(self.requests("_kiro/sdk/forkPoint")), 1)
        finally:
            await client.disconnect()


if __name__ == "__main__":
    unittest.main()
