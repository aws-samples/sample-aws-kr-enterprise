import asyncio
import copy
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (
    InMemorySessionStore,
    SQLiteSessionStore,
    list_sessions_from_store,
    merge_session_info,
    project_key_for_directory,
)


def info(**fields):
    return {"type": "session_info", "data": fields}


class SessionInfoStoreTests(unittest.IsolatedAsyncioTestCase):
    def test_merge_is_pure_and_last_value_wins(self):
        first = merge_session_info(None, [{"type": "prompt", "data": "ignored"}, info(title="A", cwd="/w")])
        original = copy.deepcopy(first)
        second = merge_session_info(first, [info(title="B"), {"type": "session_info", "data": "bad"}])
        self.assertEqual(first, original)
        self.assertEqual(second, {"title": "B", "cwd": "/w"})

    async def test_fresh_index_avoids_loading_and_stale_index_falls_back(self):
        class CountingStore(InMemorySessionStore):
            def __init__(self):
                super().__init__()
                self.loads = []
                self.stale = False

            async def list_session_info(self, project_key):
                result = await super().list_session_info(project_key)
                if self.stale:
                    for record in result:
                        record["mtime"] = 0
                return result

            async def load(self, key):
                self.loads.append(key["session_id"])
                return await super().load(key)

        store = CountingStore()
        project = project_key_for_directory(".")
        for sid in ("first", "second"):
            await store.append({"project_key": project, "session_id": sid}, [info(title=sid)])
        await store.append({"project_key": project, "session_id": "second"}, [])
        fresh = await list_sessions_from_store(store, directory=".", limit=1)
        self.assertEqual(fresh[0].title, "second")
        self.assertEqual(store.loads, [])
        store.stale = True
        stale = await list_sessions_from_store(store, directory=".", limit=1)
        self.assertEqual(stale, fresh)
        self.assertEqual(store.loads, ["second"])

    async def test_sqlite_index_is_atomic_with_writes_and_cascading_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteSessionStore(Path(directory) / "sessions.db")
            key = {"project_key": project_key_for_directory(directory), "session_id": "s"}
            await store.append(key, [info(sessionId="s", title="First", cwd=directory)])
            await store.append({**key, "subpath": "child"}, [info(title="Child")])
            await asyncio.gather(*(store.append(key, [info(title=str(i))]) for i in range(20)))
            records = await store.load(key)
            entries = await store.list_sessions(key["project_key"])
            indexed = await SQLiteSessionStore(store.path).list_session_info(key["project_key"])
            self.assertEqual(len(indexed), 1)
            self.assertEqual(indexed[0]["mtime"], entries[0]["mtime"])
            self.assertEqual(indexed[0]["info"], {"sessionId": "s", "cwd": directory,
                                                  "title": records[-1]["data"]["title"]})
            listed = (await list_sessions_from_store(store, directory=directory))[0]
            self.assertEqual((listed.title, listed.cwd), (records[-1]["data"]["title"], directory))
            await store.delete(key)
            self.assertEqual(await store.list_session_info(key["project_key"]), [])
            self.assertIsNone(await store.load({**key, "subpath": "child"}))

    async def test_index_only_store(self):
        class IndexOnlyStore:
            async def list_session_info(self, project_key):
                return [{"session_id": "s", "mtime": 7, "info": {"title": "Indexed"}}]

        result = await list_sessions_from_store(IndexOnlyStore())
        self.assertEqual((result[0].session_id, result[0].title, result[0].updated_at), ("s", "Indexed", 7))


if __name__ == "__main__":
    unittest.main()
