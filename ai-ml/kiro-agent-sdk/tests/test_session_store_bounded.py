import tempfile
import unittest
import json
import random
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (InMemorySessionStore, SQLiteSessionStore, get_session_messages_from_store,
                            project_key_for_directory)
from kiro_agent_sdk.archive import _archive_payload
from kiro_agent_sdk.sqlite_store import _preflight_json_structure
from history_helpers import blocks, role


class BoundedSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_limits_and_validation(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "bounded.db")):
                key = {"project_key": "p", "session_id": "s"}
                await store.append(key, [{"type": "prompt", "data": "first"}, {"type": "result", "data": "second"}])
                self.assertEqual(len(await store.load_bounded(key, max_entries=2, max_bytes=1000)), 2)
                self.assertIsNone(await store.load_bounded({"project_key": "p", "session_id": "missing"},
                                                           max_entries=2, max_bytes=1000))
                for count, size in ((1, 1000), (2, 2)):
                    with self.assertRaises(ValueError):
                        await store.load_bounded(key, max_entries=count, max_bytes=size)
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=True, max_bytes=1000)
                complex_key = {"project_key": "p", "session_id": "complex"}
                await store.append(complex_key, [
                    {"type": "synthetic", "data": list(range(1024))} for _ in range(200)])
                with self.assertRaisesRegex(ValueError, "structural"):
                    await store.load_bounded(complex_key, max_entries=250, max_bytes=8 * 1024 * 1024)

    async def test_rejects_cycles_and_corrupted_deep_json(self):
        memory = InMemorySessionStore()
        key = {"project_key": "p", "session_id": "s"}
        cycle = {"type": "synthetic"}
        cycle["data"] = cycle
        await memory.append(key, [cycle])
        with self.assertRaises(ValueError):
            await memory.load_bounded(key, max_entries=10, max_bytes=10000)
        with tempfile.TemporaryDirectory() as root:
            sqlite = SQLiteSessionStore(Path(root) / "invalid.db")
            await sqlite.append(key, [{"type": "synthetic"}])
            connection = sqlite._connect()
            try:
                connection.execute("UPDATE kiro_sdk_entries SET entry_json = ?", ("[" * 2000 + "0" + "]" * 2000,))
            finally:
                connection.close()
            with self.assertRaises(ValueError):
                await sqlite.load_bounded(key, max_entries=10, max_bytes=10000)

    async def test_archive_markers_preserve_boundaries_and_bound_relevant_entries(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "archive.db")):
                key = {"project_key": "p", "session_id": "s"}
                archive = {"type": "session_archive", "data": _archive_payload("s", b"a" * 16384)}
                entries = [{"type": "prompt", "data": [{"type": "text", "text": "question"}]},
                           archive, {"type": "result", "data": {}}]
                await store.append(key, entries)
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=3, max_bytes=1024)
                snapshot = await store.load_bounded(key, max_entries=3, max_bytes=1024, skip_archives=True)
                self.assertEqual(snapshot, [entries[0], {"type": "session_archive"}, entries[2]])
                self.assertEqual(await store.load(key), entries)
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=3, max_bytes=1024, skip_archives="true")
                await store.append(key, [{"type": "prompt", "data": "x" * 2000}])
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=4, max_bytes=1024, skip_archives=True)

    async def test_noncanonical_large_archive_is_not_silently_skipped_in_sqlite(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteSessionStore(Path(root) / "archive.db")
            key = {"project_key": "p", "session_id": "s"}
            archive = {"type": "session_archive", "data": _archive_payload("s", b"a" * 16384),
                       "unexpected": True}
            await store.append(key, [archive])
            with self.assertRaises(ValueError):
                await store.load_bounded(key, max_entries=1, max_bytes=1024, skip_archives=True)

    async def test_archive_scan_is_bounded_and_duplicate_type_cannot_hide_a_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteSessionStore(Path(root) / "archive.db")
            key = {"project_key": "p", "session_id": "s"}
            await store.append(key, [{"type": "session_archive", "data": _archive_payload("s", b"a" * 4096)}])
            with patch("kiro_agent_sdk.sqlite_store._MAX_ARCHIVE_SCAN_BYTES", 1024):
                with self.assertRaisesRegex(ValueError, "scan"):
                    await store.load_bounded(key, max_entries=1, max_bytes=1024, skip_archives=True)
            connection = store._connect()
            try:
                original = connection.execute("SELECT entry_json FROM kiro_sdk_entries").fetchone()[0]
                connection.execute("UPDATE kiro_sdk_entries SET entry_json = ?",
                                   (original[:-1] + ', "type": "prompt"}',))
            finally:
                connection.close()
            with self.assertRaises(ValueError):
                await store.load_bounded(key, max_entries=1, max_bytes=1024, skip_archives=True)

    async def test_archive_record_limit_applies_even_when_max_bytes_is_larger(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteSessionStore(Path(root) / "archive.db")
            key = {"project_key": "p", "session_id": "s"}
            await store.append(key, [{"type": "archive_message", "data": {"content": "x" * 2000}}])
            with patch("kiro_agent_sdk.sqlite_store._MAX_ARCHIVE_ENTRY_BYTES", 1024):
                with self.assertRaisesRegex(ValueError, "archive"):
                    await store.load_bounded(key, max_entries=1, max_bytes=4096, skip_archives=True)

    async def test_skip_does_not_call_untrusted_type_equality(self):
        class UntrustedType(str):
            def __eq__(self, other):
                raise AssertionError("untrusted equality must not be invoked")
            __hash__ = str.__hash__
        store = InMemorySessionStore()
        key = {"project_key": "p", "session_id": "s"}
        store._entries[("p", "s", "")] = [{"type": UntrustedType("session_archive"), "data": "private"}]
        with self.assertRaises(ValueError):
            await store.load_bounded(key, max_entries=1, max_bytes=1024, skip_archives=True)

    async def test_bounded_message_projection_matches_full_projection(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "messages.db")):
                key = {"project_key": project_key_for_directory(root), "session_id": "s"}
                prompt_key = "a" * 32
                await store.append(key, [
                    {"type": "prompt", "kiro_sdk_prompt_key": prompt_key,
                     "data": [{"type": "text", "text": "question"}]},
                    {"type": "update", "kiro_sdk_prompt_key": prompt_key,
                     "data": {"sessionUpdate": "tool_call", "toolCallId": "call", "title": "probe",
                              "rawInput": {"value": "synthetic"}}},
                    {"type": "session_archive", "data": _archive_payload("s", b"a" * 16384)},
                    {"type": "update", "kiro_sdk_prompt_key": prompt_key,
                     "data": {"sessionUpdate": "tool_call_update", "toolCallId": "call", "status": "completed",
                              "rawOutput": "result"}},
                    {"type": "result", "kiro_sdk_prompt_key": prompt_key, "data": {}},
                ])
                kwargs = {"directory": root, "include_observed_tool_messages": True}
                original = await get_session_messages_from_store(store, "s", **kwargs)
                with patch.object(store, "load", side_effect=AssertionError("full read was used")):
                    bounded = await get_session_messages_from_store(
                        store, "s", bounded_without_archives=True, **kwargs)
                self.assertEqual(bounded, original)
                self.assertEqual(blocks(bounded[0]), [{"type": "text", "text": "question"}])
                tool = next(item for item in bounded if role(item) == "tool")
                self.assertEqual((tool.status, tool.raw_input, tool.raw_output), ("completed", {"value": "synthetic"}, "result"))
                with self.assertRaises(ValueError):
                    await get_session_messages_from_store(store, "s", bounded_without_archives=1, **kwargs)
            with self.assertRaises(ValueError):
                await get_session_messages_from_store(object(), "s", directory=root, bounded_without_archives=True)

    async def test_imported_archive_records_are_boundary_markers(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "imported.db")):
                key = {"project_key": project_key_for_directory(root), "session_id": "s"}
                records = [
                    {"type": "update", "data": {"sessionUpdate": "agent_message_chunk", "messageId": "one",
                                                 "content": {"type": "text", "text": "first"}}},
                    {"type": "archive_message", "data": {"id": "synthetic", "payload": {"content": "x" * 600}}},
                    {"type": "archive_message", "data": {"id": "synthetic2", "payload": {"content": "y" * 600}}},
                    {"type": "update", "data": {"sessionUpdate": "agent_message_chunk", "messageId": "one",
                                                 "content": {"type": "text", "text": "second"}}},
                ]
                await store.append(key, records)
                with self.assertRaises(ValueError):
                    await store.load_bounded(key, max_entries=4, max_bytes=1024)
                snapshot = await store.load_bounded(key, max_entries=4, max_bytes=1024, skip_archives=True)
                self.assertEqual(snapshot[1:3], [{"type": "archive_message"}, {"type": "archive_message"}])
                full = await get_session_messages_from_store(store, "s", directory=root, coalesce_chunks=True)
                bounded = await get_session_messages_from_store(
                    store, "s", directory=root, coalesce_chunks=True, bounded_without_archives=True)
                self.assertEqual(bounded, full)
                self.assertEqual(len(bounded), 2)

    async def test_sqlite_rejects_excess_structure_before_json_decoding(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteSessionStore(Path(root) / "structure.db")
            key = {"project_key": "p", "session_id": "s"}
            await store.append(key, [{"type": "prompt", "data": [0] * 100000}])
            with patch("kiro_agent_sdk.sqlite_store.json.loads",
                       side_effect=AssertionError("oversized JSON must not be decoded")):
                with self.assertRaisesRegex(ValueError, "structural"):
                    await store.load_bounded(key, max_entries=1, max_bytes=1024 * 1024, skip_archives=True)

    async def test_sqlite_preflight_preserves_escaped_text(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteSessionStore(Path(root) / "strings.db")
            key = {"project_key": "p", "session_id": "s"}
            entries = [{"type": "prompt", "data": ['quoted "{[\\]}": value', "\\n\\t:,[", "\n{[}]"]}]
            await store.append(key, entries)
            for skip in (False, True):
                self.assertEqual(await store.load_bounded(key, max_entries=1, max_bytes=4096, skip_archives=skip),
                                 entries)

    def test_preflight_accepts_varied_valid_json_strings(self):
        randomizer = random.Random(4)
        alphabet = 'ab"\\{}[],:한\n\t'
        def value(depth):
            if depth <= 0 or randomizer.randrange(3) == 0:
                return "".join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(30)))
            if randomizer.randrange(2):
                return [value(depth - 1) for _ in range(randomizer.randrange(4))]
            return {f"{number}": value(depth - 1) for number in range(randomizer.randrange(4))}
        for _ in range(500):
            text = "".join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(200)))
            entry = {"type": "prompt", "data": [{"text": text}, {"nested": [text, 1, None]}, value(5)]}
            _preflight_json_structure(json.dumps(entry, ensure_ascii=False), 20000)
