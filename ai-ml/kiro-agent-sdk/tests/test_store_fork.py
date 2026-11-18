import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (ForkSessionResult, InMemorySessionStore, SessionArchiveError, StoreForkError,
                            fork_session_via_store, get_session_info_from_store, project_key_for_directory)
from kiro_agent_sdk.archive import _archive_payload
from kiro_agent_sdk.store_fork import _verify_isolation

FAKE = os.path.join(os.path.dirname(__file__), 'fake_kiro.py')


def source_archive():
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('session.json', '{}')
        archive.writestr('messages.jsonl', json.dumps({'id': 'u1', 'payload': {'type': 'user', 'content': 'hello'}}) + '\n')
    return output.getvalue()


class StoreForkTests(unittest.IsolatedAsyncioTestCase):
    async def test_fork_stays_in_isolated_home_and_is_written_to_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InMemorySessionStore()
            key = {'project_key': project_key_for_directory(directory), 'session_id': 'source'}
            await store.append(key, [{'type': 'session_archive', 'data': _archive_payload('source', source_archive())}])
            with patch('kiro_agent_sdk.store_fork._verify_isolation') as verify, \
                 patch('kiro_agent_sdk.store_fork.import_session_archive', return_value='imported') as load, \
                 patch('kiro_agent_sdk.store_fork.fork_session', return_value=ForkSessionResult('forked')) as fork, \
                 patch('kiro_agent_sdk.store_fork.export_session_archive', return_value=source_archive()):
                result = await fork_session_via_store(store, 'source', directory, 'u1', '  Branch  ', cli_path=FAKE, batch_size=1)
            self.assertEqual(result.session_id, 'forked')
            self.assertEqual(fork.await_args.kwargs['up_to_message_id'], 'u1')
            isolated_env = load.await_args.kwargs['env']
            self.assertEqual(isolated_env['KIRO_HOME'], str(Path(isolated_env['HOME']) / '.kiro'))
            self.assertFalse(Path(isolated_env['HOME']).exists())
            verify.assert_awaited_once()
            self.assertEqual((await get_session_info_from_store(store, 'forked', directory)).title, 'Branch')
            entries = await store.load({'project_key': key['project_key'], 'session_id': 'forked'})
            self.assertEqual(entries[-1]['type'], 'session_archive')

    async def test_stale_or_unsupported_version_has_no_store_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = InMemorySessionStore()
            key = {'project_key': project_key_for_directory(directory), 'session_id': 'source'}
            await store.append(key, [{'type': 'session_archive', 'data': _archive_payload('source', source_archive())}])
            with self.assertRaisesRegex(SessionArchiveError, 'tested Kiro CLI'):
                await fork_session_via_store(store, 'source', directory, cli_path=FAKE,
                                             env={'FAKE_VERSION': 'kiro-cli 9.0'})
            self.assertEqual(len(await store.list_sessions(key['project_key'])), 1)
            await store.append(key, [{'type': 'prompt', 'data': []}])
            with patch('kiro_agent_sdk.store_fork._require_tested_cli') as check:
                with self.assertRaisesRegex(SessionArchiveError, 'activity after'):
                    await fork_session_via_store(store, 'source', directory, cli_path=FAKE)
                check.assert_not_awaited()

    async def test_store_error_exposes_created_fork_id(self):
        class BrokenStore:
            async def load(self, key):
                return [{'type': 'session_archive', 'data': _archive_payload('source', source_archive())}]
            async def append_batches_if_absent(self, key, batches):
                raise RuntimeError('storage unavailable')
        with tempfile.TemporaryDirectory() as directory:
            with patch('kiro_agent_sdk.store_fork._verify_isolation'), \
                 patch('kiro_agent_sdk.store_fork.import_session_archive', return_value='imported'), \
                 patch('kiro_agent_sdk.store_fork.fork_session', return_value=ForkSessionResult('forked')), \
                 patch('kiro_agent_sdk.store_fork.export_session_archive', return_value=source_archive()):
                with self.assertRaises(StoreForkError) as raised:
                    await fork_session_via_store(BrokenStore(), 'source', directory, cli_path=FAKE)
                self.assertEqual(raised.exception.fork_session_id, 'forked')
                self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    async def test_isolation_failure_cleans_up_only_new_probe(self):
        class Probe:
            session_id = 'probe'
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None
        calls = 0
        async def listing(*, cwd, cli_path, env):
            nonlocal calls
            calls += 1
            if calls in (1, 5):
                return []
            return [{'sessionId': 'probe'}]
        with patch('kiro_agent_sdk.store_fork.KiroSDKClient', return_value=Probe()), \
             patch('kiro_agent_sdk.store_fork.list_sessions', side_effect=listing), \
             patch('kiro_agent_sdk.store_fork._run_cli', return_value=b'') as run:
            with self.assertRaisesRegex(SessionArchiveError, 'isolation'):
                await _verify_isolation('/tmp', FAKE, None, {'KIRO_HOME': '/tmp/isolated'}, 5)
            self.assertEqual(run.await_args.args[0], ['--delete-session', 'probe'])

    async def test_isolation_failure_never_deletes_preexisting_id(self):
        class Probe:
            session_id = 'existing'
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None
        async def listing(*, cwd, cli_path, env):
            return [{'sessionId': 'existing'}]
        with patch('kiro_agent_sdk.store_fork.KiroSDKClient', return_value=Probe()), \
             patch('kiro_agent_sdk.store_fork.list_sessions', side_effect=listing), \
             patch('kiro_agent_sdk.store_fork._run_cli', return_value=b'') as run:
            with self.assertRaisesRegex(SessionArchiveError, 'isolation'):
                await _verify_isolation('/tmp', FAKE, None, {'KIRO_HOME': '/tmp/isolated'}, 5)
            run.assert_not_awaited()

    async def test_isolation_probe_cleanup_must_be_confirmed(self):
        class Probe:
            session_id = 'probe'
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None
        calls = 0
        async def listing(*, cwd, cli_path, env):
            nonlocal calls
            calls += 1
            return [] if calls == 1 else [{'sessionId': 'probe'}]
        with patch('kiro_agent_sdk.store_fork.KiroSDKClient', return_value=Probe()), \
             patch('kiro_agent_sdk.store_fork.list_sessions', side_effect=listing), \
             patch('kiro_agent_sdk.store_fork._run_cli', return_value=b''):
            with self.assertRaisesRegex(SessionArchiveError, 'verify cleanup'):
                await _verify_isolation('/tmp', FAKE, None, {'KIRO_HOME': '/tmp/isolated'}, 5)
