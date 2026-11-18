import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import SQLiteSessionStore


class SQLiteSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_order_and_cascades_subkeys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sessions.db'
            key = {'project_key': 'project', 'session_id': 'session'}
            subkey = {**key, 'subpath': 'subagents/worker'}
            store = SQLiteSessionStore(path)
            await store.append(key, [{'type': 'first'}, {'type': 'second'}])
            await store.append(subkey, [{'type': 'child'}])
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)
            reopened = SQLiteSessionStore(path)
            self.assertEqual([e['type'] for e in await reopened.load(key)], ['first', 'second'])
            self.assertEqual(await reopened.list_subkeys(key), ['subagents/worker'])
            self.assertEqual((await reopened.list_sessions('project'))[0]['session_id'], 'session')
            await reopened.delete(key)
            self.assertIsNone(await reopened.load(key))
            self.assertIsNone(await reopened.load(subkey))

    async def test_concurrent_batches_remain_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sessions.db'
            stores = [SQLiteSessionStore(path), SQLiteSessionStore(path)]
            key = {'project_key': 'project', 'session_id': 'session'}
            async def write(index):
                await stores[index % 2].append(key, [{'index': index, 'part': 1}, {'index': index, 'part': 2}])
            await asyncio.gather(*(write(i) for i in range(30)))
            entries = await stores[0].load(key)
            self.assertEqual(len(entries), 60)
            self.assertEqual({entry['index'] for entry in entries}, set(range(30)))
            for first, second in zip(entries[::2], entries[1::2]):
                self.assertEqual((first['index'], first['part'], second['index'], second['part']),
                                 (first['index'], 1, first['index'], 2))

    async def test_separate_processes_can_write_same_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'sessions.db')
            program = '''import asyncio, sys
from kiro_agent_sdk import SQLiteSessionStore
async def main():
    store = SQLiteSessionStore(sys.argv[1])
    for i in range(10):
        await store.append({'project_key':'p','session_id':'s'}, [{'writer':sys.argv[2], 'i':i}])
asyncio.run(main())
'''
            processes = [await asyncio.create_subprocess_exec(sys.executable, '-c', program, path, name,
                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                         for name in ('a', 'b')]
            for process in processes:
                _, stderr = await asyncio.wait_for(process.communicate(), 30)
                self.assertEqual(process.returncode, 0, stderr.decode())
            entries = await SQLiteSessionStore(path).load({'project_key': 'p', 'session_id': 's'})
            self.assertEqual(len(entries), 20)
            for writer in ('a', 'b'):
                self.assertEqual([e['i'] for e in entries if e['writer'] == writer], list(range(10)))

    async def test_invalid_entries_do_not_partially_append(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteSessionStore(Path(directory) / 'sessions.db')
            key = {'project_key': 'p', 'session_id': 's'}
            await store.append(key, [{'type': 'before'}])
            with self.assertRaises(ValueError):
                await store.append(key, [{'type': 'new'}, {'value': float('nan')}])
            self.assertEqual(await store.load(key), [{'type': 'before'}])
            await store.append({'project_key': 'p', 'session_id': 'empty'}, [])
            self.assertEqual(await store.load({'project_key': 'p', 'session_id': 'empty'}), [])
            with self.assertRaises(ValueError):
                SQLiteSessionStore(':memory:')

    async def test_append_if_absent_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sessions.db'
            stores = [SQLiteSessionStore(path), SQLiteSessionStore(path)]
            key = {'project_key': 'p', 'session_id': 's'}
            results = await asyncio.gather(*(stores[i % 2].append_if_absent(key, [{'writer': i}])
                                             for i in range(20)))
            self.assertEqual(sum(results), 1)
            self.assertEqual(len(await stores[0].load(key)), 1)

    async def test_batched_create_validates_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteSessionStore(Path(directory) / 'sessions.db')
            key = {'project_key': 'p', 'session_id': 's'}
            with self.assertRaises(ValueError):
                await store.append_batches_if_absent(key, [[{'type': 'first'}], [{'value': float('nan')}]])
            self.assertIsNone(await store.load(key))
            self.assertTrue(await store.append_batches_if_absent(key, [[{'n': 1}], [{'n': 2}]]))
            self.assertFalse(await store.append_batches_if_absent(key, [[{'n': 3}]]))
            self.assertEqual(await store.load(key), [{'n': 1}, {'n': 2}])
