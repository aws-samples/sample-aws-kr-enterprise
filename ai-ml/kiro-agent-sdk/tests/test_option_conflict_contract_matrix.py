"""Conflicting option combinations are rejected, and dependent options only act together.

Each rejected combination also has an enabling configuration that constructs, so the error is
caused by the conflict and not by the value itself.
"""
import asyncio
import base64
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import (InMemorySessionStore, KiroAgentOptions, KiroSDKClient,
                            SessionArchiveError, StreamEvent,
                            project_key_for_directory, tool)
from kiro_agent_sdk.archive import ARCHIVE_FORMAT

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


async def callback(*args):
    return None


@tool("check", "Check", {})
async def check(args):
    return {"content": [{"type": "text", "text": "{}"}]}


class OptionConflictContractMatrix(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_conflicting_combinations_are_rejected_while_the_enabling_one_constructs(self):
        v3 = dict(agent_engine="v3", cli_path=FAKE, cwd=str(self.root))
        runtime = dict(v3, runtime_compat_mode="v3")
        cases = [
            ("fallback_model_mode", "runtime", dict(runtime, model="primary"), "explicit fallback_model",
             dict(runtime, model="primary", fallback_model="secondary")),
            ("fallback_model_mode", "startup", dict(v3, fallback_model="secondary"), "requires a primary model",
             dict(v3, model="primary", fallback_model="secondary"))
        ]
        for name, value, conflicting, message, enabling in cases:
            with self.subTest(option=name, value=value, conflict=message):
                KiroSDKClient(KiroAgentOptions(**{**enabling, name: value}))
                with self.assertRaisesRegex(ValueError, message):
                    KiroSDKClient(KiroAgentOptions(**{**conflicting, name: value}))

    async def test_stream_event_options_only_act_together(self):
        async def events(prompt, **values):
            options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", cwd=self.root, **values)
            async with KiroSDKClient(options) as client:
                await client.query(prompt)
                return [message async for message in client.receive_response()]

        def streams(messages, child=False):
            return [m for m in messages if isinstance(m, StreamEvent) and bool(m.agent_id) == child]

        self.assertEqual(streams(await events("hello")), [])
        self.assertTrue(streams(await events("hello", include_partial_messages=True)))
        self.assertEqual(streams(await events("nested", forward_subagent_text=True), child=True), [])
        self.assertEqual(streams(await events("nested", include_partial_messages=True), child=True), [])
        self.assertTrue(streams(await events("nested", forward_subagent_text=True, include_partial_messages=True),
                                child=True))

    async def test_archive_limits_and_timeout_also_bound_store_recovery(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("session.json", "{}")
            archive.writestr("messages.jsonl", '{"message":"hello"}\n' * 20)
        data = output.getvalue()
        source = self.root / "source"
        source.mkdir()
        store = InMemorySessionStore()
        await store.append({"project_key": project_key_for_directory(str(source)), "session_id": "source-id"}, [{
            "type": "session_archive", "data": {"format": ARCHIVE_FORMAT, "session_id": "source-id",
                                                "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                                "base64": base64.b64encode(data).decode()}}])

        async def hang(*args):
            await asyncio.Event().wait()
        for changes, helper, message in (({"session_store_archive_max_uncompressed_bytes": 10}, AsyncMock(),
                                          "expanded size limit"),
                                         ({"session_store_archive_timeout": 0.05}, hang, "Timed out")):
            with self.subTest(changes=changes):
                options = KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3", session_store=store,
                                           resume="source-id", resume_from_store=True,
                                           session_store_source_directory=str(source), **changes)
                client = KiroSDKClient(options)
                with patch("kiro_agent_sdk.archive._run_cli", side_effect=helper) as run:
                    try:
                        with self.assertRaisesRegex(SessionArchiveError, message):
                            await asyncio.wait_for(client.connect(), 5)
                    finally:
                        await client.disconnect()
                    self.assertEqual(run.call_count, 0 if "max_uncompressed" in str(changes) else 1)


if __name__ == "__main__":
    unittest.main()
