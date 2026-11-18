import io
import json
import tempfile
import asyncio
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (
    ForkSessionResult, InMemorySessionStore, KiroAgentOptions, KiroSDKClient, SQLiteSessionStore,
    SessionArchiveError, fork_session_via_store, get_session_archive_history_from_store,
    get_session_archive_messages_from_store, get_subagent_history_from_store,
    get_subagent_messages_from_store, list_subagents_from_store, project_key_for_directory,
    restore_session_from_store,
)
from kiro_agent_sdk.archive import _archive_payload
from test_agentcore import FAKE
from history_helpers import blocks, role


def archive_fixture():
    parent = [
        {"id": "user-id", "payload": {"type": "user", "content": "question",
                                      "images": [], "documents": [], "_meta": {}}},
        {"id": "answer-id", "payload": {"type": "assistant", "content": "answer"}},
        {"id": "tool-sub-agent-start", "payload": {"type": "sub_agent_start", "subSessionId": "child"}},
    ]
    child = [{"id": "child-answer", "payload": {"type": "assistant", "content": "child answer"}}]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("session.json", "{}")
        archive.writestr("messages.jsonl", "\n".join(json.dumps(item) for item in parent))
        archive.writestr("sub-executions/child.jsonl", "\n".join(json.dumps(item) for item in child))
    return output.getvalue(), parent


class BoundedArchiveRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def populate(self, store, root):
        key = {"project_key": project_key_for_directory(root), "session_id": "session"}
        archive, records = archive_fixture()
        prompt_key = "a" * 32
        prompt = {"type": "prompt", "kiro_sdk_prompt_key": prompt_key, "kiro_sdk_original_text_input": True,
                  "data": [{"type": "text", "text": "question"}]}
        result = {"type": "result", "kiro_sdk_prompt_key": prompt_key,
                  "data": {"kiro_sdk_unique_user_message_id": "user-id"}}
        old_archive = {"type": "session_archive", "data": _archive_payload("session", b"x" * 800000)}
        await store.append(key, [prompt, result] + [old_archive] * 9 +
                           [{"type": "session_archive", "data": _archive_payload("session", archive)}])
        return key, archive, records

    async def test_latest_archive_is_preserved_with_prior_boundaries(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "store.db")):
                key, archive, _ = await self.populate(store, root)
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=20000, max_bytes=8 * 1024 * 1024)
                entries = await store.load_bounded(key, max_entries=20000, max_bytes=8 * 1024 * 1024,
                                                   skip_archives=True, retain_latest_archive=True)
                self.assertEqual(entries[2:11], [{"type": "session_archive"}] * 9)
                self.assertEqual(entries[-1]["data"], _archive_payload("session", archive))
                self.assertEqual(len(await store.load(key)), len(entries))
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=20, max_bytes=1024, retain_latest_archive=True)
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=20, max_bytes=1024,
                                             skip_archives=True, retain_latest_archive="yes")

    async def test_bounded_archive_and_subagent_queries_preserve_semantics(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "query.db")):
                key, archive, records = await self.populate(store, root)
                with patch.object(store, "load", side_effect=AssertionError("unbounded read")):
                    raw = await get_session_archive_history_from_store(
                        store, "session", root, bounded_mirror_read=True)
                    preview = await get_session_archive_messages_from_store(
                        store, "session", root, bounded_mirror_read=True)
                    agents = await list_subagents_from_store(store, "session", root, bounded_mirror_read=True)
                    child = await get_subagent_messages_from_store(
                        store, "session", "child", root, bounded_mirror_read=True)
                    child_raw = await get_subagent_history_from_store(
                        store, "session", "child", root, bounded_mirror_read=True)
                    with patch("kiro_agent_sdk.archive.import_session_archive", return_value="restored") as restore:
                        restored = await restore_session_from_store(store, "session", root, bounded_mirror_read=True)
                self.assertEqual(raw, records)
                self.assertEqual(blocks(preview[0]), [{"type": "text", "text": "question"}])
                self.assertEqual(agents, ["child"])
                self.assertEqual(child[0].content[0].text, "child answer")
                self.assertEqual(child_raw[0]["id"], "child-answer")
                self.assertEqual(restored, "restored")
                self.assertEqual(restore.await_args.args[0], archive)
                await store.append(key, [{"type": "prompt", "data": []},
                    {"type": "update", "data": {"sessionUpdate": "tool_call", "toolCallId": "later",
                        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "later-child"}}}}])
                self.assertEqual(await list_subagents_from_store(
                    store, "session", root, bounded_mirror_read=True), ["child", "later-child"])
                with self.assertRaisesRegex(SessionArchiveError, "activity after"):
                    await get_session_archive_history_from_store(store, "session", root, bounded_mirror_read=True)
                with self.assertRaisesRegex(SessionArchiveError, "activity after"):
                    await get_subagent_messages_from_store(
                        store, "session", "child", root, bounded_mirror_read=True)
                stale = await get_session_archive_messages_from_store(
                    store, "session", root, bounded_mirror_read=True, allow_stale=True)
                self.assertEqual(blocks(stale[0]), [{"type": "text", "text": "question"}])

    async def test_last_corrupt_archive_does_not_fall_back_to_previous(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "corrupt.db")):
                key, archive, _ = await self.populate(store, root)
                damaged = _archive_payload("session", archive)
                damaged["sha256"] = "0" * 64
                await store.append(key, [{"type": "session_archive", "data": damaged}])
                with self.assertRaisesRegex(SessionArchiveError, "integrity"):
                    await get_session_archive_history_from_store(store, "session", root, bounded_mirror_read=True)
                await store.append(key, [{"type": "session_archive", "data": None}])
                with self.assertRaisesRegex(SessionArchiveError, "supported"):
                    await get_session_archive_history_from_store(store, "session", root, bounded_mirror_read=True)

    async def test_sqlite_read_uses_one_snapshot_during_concurrent_append(self):
        from kiro_agent_sdk.sqlite_store import _preflight_json_structure
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteSessionStore(Path(root) / "concurrent.db")
            key = {"project_key": project_key_for_directory(root), "session_id": "session"}
            older = _archive_payload("session", b"older")
            newer = _archive_payload("session", b"newer")
            await store.append(key, [{"type": "prompt", "data": []}, {"type": "session_archive", "data": older}])
            started, release = threading.Event(), threading.Event()
            def delayed(raw, limit):
                if not started.is_set():
                    started.set()
                    if not release.wait(10):
                        raise TimeoutError("test did not release reader")
                _preflight_json_structure(raw, limit)
            try:
                with patch("kiro_agent_sdk.sqlite_store._preflight_json_structure", delayed):
                    reading = asyncio.create_task(store.load_bounded(
                        key, max_entries=10, max_bytes=4096, skip_archives=True, retain_latest_archive=True))
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    writing = asyncio.create_task(store.append(key, [{"type": "session_archive", "data": newer}]))
                    await asyncio.sleep(0.05)
                    release.set()
                    snapshot = await reading
                    await writing
                self.assertEqual(snapshot[-1]["data"], older)
                later = await store.load_bounded(
                    key, max_entries=10, max_bytes=4096, skip_archives=True, retain_latest_archive=True)
                self.assertEqual(later[-1]["data"], newer)
            finally:
                release.set()

    async def test_no_archive_and_oversized_latest_archive_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "limits.db")):
                self.assertEqual(await get_session_archive_history_from_store(
                    store, "missing", root, bounded_mirror_read=True), [])
                key = {"project_key": project_key_for_directory(root), "session_id": "session"}
                await store.append(key, [{"type": "prompt", "data": []}])
                with self.assertRaisesRegex(SessionArchiveError, "require"):
                    await get_session_archive_history_from_store(store, "session", root, bounded_mirror_read=True)
                await store.append(key, [{"type": "session_archive", "data": _archive_payload("session", b"x" * 2048)}])
                target = ("kiro_agent_sdk.session_store._MAX_RETAINED_ARCHIVE_ENTRY_BYTES"
                          if type(store) is InMemorySessionStore else
                          "kiro_agent_sdk.sqlite_store._MAX_ARCHIVE_ENTRY_BYTES")
                with patch(target, 1024):
                    with self.assertRaisesRegex(SessionArchiveError, "Bounded"):
                        await get_session_archive_history_from_store(store, "session", root, bounded_mirror_read=True)

    async def test_event_only_mirror_still_lists_observed_subagents(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "events.db")):
                key = {"project_key": project_key_for_directory(root), "session_id": "session"}
                await store.append(key, [{"type": "update", "data": {
                    "sessionUpdate": "tool_call", "toolCallId": "call",
                    "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "child"}}}}])
                self.assertEqual(await list_subagents_from_store(store, "session", root, bounded_mirror_read=True),
                                 ["child"])
                with self.assertRaisesRegex(SessionArchiveError, "require"):
                    await get_subagent_messages_from_store(
                        store, "session", "child", root, bounded_mirror_read=True)

    async def test_sqlite_duplicate_type_cannot_hide_later_activity(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteSessionStore(Path(root) / "duplicate.db")
            key = {"project_key": project_key_for_directory(root), "session_id": "session"}
            await store.append(key, [{"type": "session_archive", "data": _archive_payload("session", archive_fixture()[0])}])
            connection = store._connect()
            try:
                original = connection.execute("SELECT entry_json FROM kiro_sdk_entries").fetchone()[0]
                connection.execute(
                    "INSERT INTO kiro_sdk_entries (project_key, session_id, subpath, entry_json) VALUES (?, ?, ?, ?)",
                    (key["project_key"], key["session_id"], "", original[:-1] + ', "type": "update"}'))
            finally:
                connection.close()
            with self.assertRaisesRegex(SessionArchiveError, "activity after"):
                await get_session_archive_history_from_store(store, "session", root, bounded_mirror_read=True)

    async def test_bounded_store_fork_uses_latest_archive(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteSessionStore(Path(root) / "fork.db")
            _, archive, _ = await self.populate(store, root)
            with patch.object(store, "load", side_effect=AssertionError("unbounded read")), \
                    patch("kiro_agent_sdk.store_fork._require_tested_cli"), \
                    patch("kiro_agent_sdk.store_fork._verify_isolation"), \
                    patch("kiro_agent_sdk.store_fork.import_session_archive", return_value="imported") as load, \
                    patch("kiro_agent_sdk.store_fork.fork_session", return_value=ForkSessionResult("forked")), \
                    patch("kiro_agent_sdk.store_fork.export_session_archive", return_value=archive):
                result = await fork_session_via_store(store, "session", root, bounded_mirror_read=True)
            self.assertEqual(result.session_id, "forked")
            self.assertEqual(load.await_args.args[0], archive)

    async def test_bounded_client_recovery_and_configuration_validation(self):
        store = InMemorySessionStore()
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            options = KiroAgentOptions(cli_path=FAKE, cwd=target, agent_engine="v3",
                                       resume="source", session_store=store, resume_from_store=True,
                                       session_store_source_directory=source, resume_from_store_bounded_read=True)
            with patch("kiro_agent_sdk.archive.restore_session_from_store", return_value="restored-id") as restore:
                async with KiroSDKClient(options) as client:
                    self.assertEqual(client.session_id, "restored-id")
                    self.assertTrue(restore.await_args.kwargs["bounded_mirror_read"])
            with self.assertRaises(ValueError):
                KiroSDKClient(KiroAgentOptions(resume_from_store_bounded_read=True))
            with self.assertRaises(ValueError):
                KiroSDKClient(KiroAgentOptions(resume_from_store_bounded_read="yes"))
            client = KiroSDKClient(options)
            options.session_store = InMemorySessionStore()
            with self.assertRaisesRegex(ValueError, "changing bounded"):
                await client.connect()

    async def test_client_preserves_imported_id_when_configuration_changes_during_restore(self):
        store = InMemorySessionStore()
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", resume="source", session_store=store,
                                   resume_from_store=True, resume_from_store_bounded_read=True)
        client = KiroSDKClient(options)
        async def mutate(*args, **kwargs):
            options.session_store = InMemorySessionStore()
            return "restored-id"
        with patch("kiro_agent_sdk.archive.restore_session_from_store", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changing bounded"):
                await client.connect()
        self.assertEqual(client.restored_session_id, "restored-id")

    async def test_reconnect_checks_guard_before_loading_restored_session(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", resume="source",
                                   session_store=InMemorySessionStore(), resume_from_store=True,
                                   resume_from_store_bounded_read=True)
        client = KiroSDKClient(options)
        allowed = True
        async def guard():
            return allowed
        client._prompt_send_guard = guard
        calls = []
        original_request = KiroSDKClient._request
        async def observe(instance, method, params, *args, **kwargs):
            calls.append(method)
            return await original_request(instance, method, params, *args, **kwargs)
        try:
            with patch("kiro_agent_sdk.archive._restore_session_from_store", return_value="restored-id"), \
                    patch.object(KiroSDKClient, "_request", observe):
                await client.connect()
                await client.disconnect()
                calls.clear()
                allowed = False
                client._prompt_send_guard = guard
                with self.assertRaisesRegex(PermissionError, "authorization"):
                    await client.connect()
            self.assertNotIn("session/load", calls)
            self.assertEqual(client.restored_session_id, "restored-id")
        finally:
            await client.disconnect()

    async def test_bounded_read_rejects_custom_store_and_invalid_flag(self):
        class CustomStore:
            async def load(self, key):
                return []
        for option in (True, "yes"):
            with self.assertRaises(ValueError):
                await get_session_archive_history_from_store(CustomStore(), "session", bounded_mirror_read=option)
