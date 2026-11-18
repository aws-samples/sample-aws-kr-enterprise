"""Runtime policy options and archive size limits reach the Kiro launch, handshake and mirror paths."""
import base64
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import InMemorySessionStore, KiroAgentOptions, KiroSDKClient, SessionArchiveError
from kiro_agent_sdk.archive import ARCHIVE_FORMAT
from test_transport import FakeTransport

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


def archive_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("session.json", "{}")
        archive.writestr("messages.jsonl", '{"message":"hello"}\n' * 20)
    return output.getvalue()


@unittest.skipUnless(os.name == "posix", "requires a local POSIX Kiro fixture")
class RuntimePolicyOptionContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.log = self.root / "requests.jsonl"
        self.source = self.root / "node_modules/@kiro/agent/dist/server/acp-server.js"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"configured runtime source")
        self.source.with_name("xhr-sync-worker.js").write_bytes(b"worker")
        self.seen = self.root / "kas-server-path"
        self.wrapper = self.root / "kiro-wrapper"
        self.wrapper.write_text(f'#!/bin/sh\nprintf "%s" "$KIRO_KAS_SERVER_PATH" > {self.seen}\n'
                                f'exec {sys.executable} {FAKE} "$@"\n')
        self.wrapper.chmod(self.wrapper.stat().st_mode | stat.S_IXUSR)

    def options(self, **changes):
        values = dict(cwd=self.root, cli_path=str(self.wrapper), agent_engine="v3", tools=[],
                      env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_LOG_REQUESTS": str(self.log),
                           "FAKE_RUNTIME_POLICIES": "1"})
        values.update(changes)
        return KiroAgentOptions(**values)

    def policies(self):
        initialize = next(json.loads(line) for line in self.log.read_text().splitlines()
                          if json.loads(line).get("method") == "initialize")
        return initialize["params"]["clientCapabilities"].get("_meta", {}).get("kiro", {}).get("sdkSessionPolicies")

    async def test_runtime_compat_mode_launches_the_patched_runtime_and_negotiates_policies(self):
        with patch("kiro_agent_sdk.native_hook_runtime.patch_runtime", return_value=b"patched runtime") as patcher:
            async with KiroSDKClient(self.options(runtime_compat_mode="v3", native_hook_runtime_path=self.source)):
                self.assertEqual(patcher.call_args.args[0], b"configured runtime source")
                launched = Path(self.seen.read_text())
                self.assertEqual(launched.name, "acp-server.js")
                self.assertEqual(launched.with_name("acp-server-implementation.js").read_bytes(), b"patched runtime")
                self.assertEqual(self.policies()["protocol"], 3)
                self.assertFalse(self.policies()["postToolOutput"])
        self.assertFalse(launched.parent.exists())
        self.log.unlink()
        with patch("kiro_agent_sdk.native_hook_runtime.patch_runtime") as patcher:
            async with KiroSDKClient(self.options()):
                patcher.assert_not_called()
                self.assertEqual(self.seen.read_text(), "")
                self.assertIsNone(self.policies())

    async def test_runtime_settings_conflicts_fail_before_launch(self):
        for changes in ({"agent_engine": "v2"}, {"native_hook_bridge_mode": "same-user"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "Runtime policies require"):
                KiroSDKClient(self.options(runtime_compat_mode="v3", native_hook_runtime_path=self.source, **changes))
        transport = FakeTransport()
        with self.assertRaisesRegex(ValueError, "Runtime policies require"):
            KiroSDKClient(KiroAgentOptions(agent_engine="v3", runtime_compat_mode="v3"), transport=transport)
        self.assertEqual(transport.requests, [])
        with self.assertRaisesRegex(ValueError, "requires runtime-acp"):
            KiroSDKClient(self.options(native_hook_runtime_path=self.source))
        for runtime, message, probed in ((self.root / "acp-server.js", "existing @kiro/agent", False),
                                         (self.source.with_name("missing.js"), "existing @kiro/agent", False),
                                         (self.source, "Untested", True)):
            with self.subTest(runtime=runtime):
                self.seen.unlink(missing_ok=True)
                client = KiroSDKClient(self.options(runtime_compat_mode="v3", native_hook_runtime_path=runtime))
                try:
                    with self.assertRaisesRegex(ValueError, message):
                        await client.connect()
                finally:
                    await client.disconnect()
                self.assertFalse(self.log.exists())
                # A malformed path is rejected before the CLI version probe; an untested bundle needs the probe.
                self.assertEqual(self.seen.exists(), probed)
        home = self.root / "home"
        client = KiroSDKClient(self.options(runtime_compat_mode="v3",
                                            env={"HOME": str(home), "XDG_DATA_HOME": str(home / "data")}))
        try:
            with self.assertRaisesRegex(ValueError, "Set native_hook_runtime_path"):
                await client.connect()
        finally:
            await client.disconnect()


class ArchiveLimitOptionContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.archive = archive_bytes()
        self.imports = []

    async def helper(self, arguments, cwd, cli_path, env):
        if arguments[1] == "export-session":
            Path(arguments[arguments.index("--out") + 1]).write_bytes(self.archive)
            return json.dumps({"kind": "exportSession", "data": {"path": "unused"}}).encode()
        self.imports.append(arguments)
        return json.dumps({"kind": "importSession", "data": {"path": "/kiro/restored-id"}}).encode()

    def archives(self, store):
        return [entry for rows in store._entries.values() for entry in rows if entry["type"] == "session_archive"]

    async def test_max_bytes_bounds_the_mirrored_archive(self):
        for limit, error in ((len(self.archive) - 1, "size limit"), (len(self.archive), None)):
            with self.subTest(limit=limit):
                store = InMemorySessionStore()
                options = KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3", session_store=store,
                                           session_store_archive=True, session_store_archive_max_bytes=limit)
                with patch("kiro_agent_sdk.archive._run_cli", side_effect=self.helper):
                    async with KiroSDKClient(options) as client:
                        await client.query("hello")
                        if error:
                            with self.assertRaisesRegex(SessionArchiveError, error):
                                _ = [message async for message in client.receive_response()]
                        else:
                            self.assertEqual([m async for m in client.receive_response()][-1].result, "hello")
                self.assertEqual(len(self.archives(store)), 0 if error else 1)

    async def test_max_bytes_bounds_store_recovery_before_import(self):
        source = self.root / "source"
        source.mkdir()
        store = InMemorySessionStore()
        from kiro_agent_sdk import project_key_for_directory
        await store.append({"project_key": project_key_for_directory(str(source)), "session_id": "source-id"}, [{
            "type": "session_archive", "data": {
                "format": ARCHIVE_FORMAT, "session_id": "source-id", "size": len(self.archive),
                "sha256": hashlib.sha256(self.archive).hexdigest(),
                "base64": base64.b64encode(self.archive).decode()}}])
        for limit, restored in ((len(self.archive) - 1, False), (len(self.archive), True)):
            with self.subTest(limit=limit):
                options = KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3", session_store=store,
                                           resume="source-id", resume_from_store=True,
                                           session_store_source_directory=str(source),
                                           session_store_archive_max_bytes=limit)
                client = KiroSDKClient(options)
                with patch("kiro_agent_sdk.archive._run_cli", side_effect=self.helper):
                    try:
                        if restored:
                            await client.connect()
                            self.assertEqual(client.restored_session_id, "restored-id")
                        else:
                            with self.assertRaisesRegex(SessionArchiveError, "size limit"):
                                await client.connect()
                    finally:
                        await client.disconnect()
                self.assertEqual(len(self.imports), 1 if restored else 0)


if __name__ == "__main__":
    unittest.main()
