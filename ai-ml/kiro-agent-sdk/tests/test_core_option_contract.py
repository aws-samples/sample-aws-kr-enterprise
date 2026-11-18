"""Working directory, CLI path and startup/load timeout option contracts."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import CLIConnectionError, CLINotFoundError, KiroAgentOptions, KiroSDKClient
from test_transport import FakeTransport

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class CoreOptionContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.log = self.root / "requests.jsonl"
        self.env = {"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log)}

    async def test_cwd_selects_the_session_directory_and_invalid_directories_fail_before_launch(self):
        work = self.root / "work"
        work.mkdir()
        async with KiroSDKClient(KiroAgentOptions(cwd=work, cli_path=FAKE, agent_engine="v3", env=self.env)) as client:
            self.assertEqual(client.session_info["received"]["cwd"], str(work))
        self.log.unlink()
        for value in (self.root / "missing", Path(FAKE), "bad\0directory"):
            with self.subTest(cwd=str(value)[:30]):
                client = KiroSDKClient(KiroAgentOptions(cwd=value, cli_path=FAKE, env=self.env))
                with self.assertRaisesRegex(CLIConnectionError, "Working directory does not exist"):
                    await client.connect()
                self.assertFalse(client._connected)
                self.assertFalse(self.log.exists())

    @unittest.skipUnless(os.name == "posix", "requires an executable wrapper script")
    async def test_cli_path_runs_the_selected_executable_and_missing_paths_fail_before_launch(self):
        marker = self.root / "invoked"
        wrapper = self.root / "kiro-wrapper"
        wrapper.write_text(f"#!/bin/sh\necho invoked >> {marker}\nexec {sys.executable} {FAKE} \"$@\"\n")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        async with KiroSDKClient(KiroAgentOptions(cwd=self.root, cli_path=str(wrapper), env=self.env)) as client:
            await client.query("hello")
            self.assertEqual([m async for m in client.receive_response()][-1].result, "hello")
        self.assertEqual(marker.read_text().splitlines(), ["invoked"])
        self.log.unlink()
        client = KiroSDKClient(KiroAgentOptions(cwd=self.root, cli_path=str(self.root / "missing-kiro"), env=self.env))
        with self.assertRaises(CLINotFoundError):
            await client.connect()
        self.assertFalse(self.log.exists())
        self.assertEqual(marker.read_text().splitlines(), ["invoked"])

    async def test_custom_transport_takes_precedence_over_a_local_cli_path(self):
        transport = FakeTransport()
        options = KiroAgentOptions(agent_engine="v3", cli_path=str(self.root / "never-run"))
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query("hello")
            self.assertEqual([m async for m in client.receive_response()][-1].result, "hello")
        self.assertEqual([row["method"] for row in transport.requests][:2], ["initialize", "session/new"])
        self.assertIsNone(client._process)

    def test_startup_and_load_timeouts_reject_non_positive_or_non_numeric_values(self):
        for value in (0, -1, True, float("inf"), float("nan"), "1"):
            with self.subTest(startup_timeout=value), self.assertRaisesRegex(ValueError, "startup_timeout"):
                KiroSDKClient(KiroAgentOptions(startup_timeout=value))
        KiroSDKClient(KiroAgentOptions(startup_timeout=None))
        for value in (0, -1, True, 1.5, float("inf"), float("nan"), "100"):
            with self.subTest(load_timeout_ms=value), self.assertRaisesRegex(ValueError, "load_timeout_ms"):
                KiroSDKClient(KiroAgentOptions(load_timeout_ms=value))
        KiroSDKClient(KiroAgentOptions(load_timeout_ms=1))


if __name__ == "__main__":
    unittest.main()
