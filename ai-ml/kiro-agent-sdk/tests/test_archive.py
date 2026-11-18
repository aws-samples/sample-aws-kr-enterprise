import asyncio
import base64
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (InMemorySessionStore, KiroAgentOptions, KiroSDKClient, SQLiteSessionStore,
                            ProcessError, ProtocolError, SessionArchiveError, export_session_archive, import_session_archive,
                            get_session_messages_from_store, import_session_to_store, list_sessions_from_store,
                            project_key_for_directory, restore_session_from_store)
from kiro_agent_sdk.archive import ARCHIVE_FORMAT
from history_helpers import blocks, role

FAKE = os.path.join(os.path.dirname(__file__), 'fake_kiro.py')


def archive_bytes(extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('session.json', '{}')
        archive.writestr('messages.jsonl', '{"message":"hello"}\n')
        if extra is not None:
            archive.writestr(*extra)
    return output.getvalue()


def transcript_archive(with_subagent=False):
    output = io.BytesIO()
    messages = [
        {'id': 'user-1', 'payload': {'type': 'user', 'content': 'Remember example'}},
        {'id': 'thought', 'payload': {'type': 'assistant', 'operationType': 'Reasoning', 'content': 'hidden'}},
        {'id': 'assistant-1', 'payload': {'type': 'assistant', 'operationType': 'Say', 'content': 'OK'}},
    ]
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('session.json', '{}')
        archive.writestr('messages.jsonl', '\n'.join(json.dumps(item) for item in messages) + '\n')
        if with_subagent:
            archive.writestr('sub-executions/child-1.jsonl', '{"id":"child","payload":{"type":"assistant"}}\n')
    return output.getvalue()


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_imported_archive_supports_bounded_message_projection(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (InMemorySessionStore(), SQLiteSessionStore(Path(root) / "bounded.db")):
                with patch('kiro_agent_sdk.archive.export_session_archive', return_value=transcript_archive()):
                    await import_session_to_store('source', store, directory=root)
                expected = await get_session_messages_from_store(store, 'source', directory=root)
                actual = await get_session_messages_from_store(
                    store, 'source', directory=root, bounded_without_archives=True)
                self.assertEqual(actual, expected)
                self.assertEqual([item.message_id for item in actual], ['user-1', 'assistant-1'])
                key = {'project_key': project_key_for_directory(root), 'session_id': 'source'}
                self.assertEqual((await store.load(key))[-1]['type'], 'session_archive')

    async def test_import_records_the_kiro_session_metadata_from_the_archive(self):
        output = io.BytesIO()
        session = {'id': 'source', 'title': 'Kiro Generated Title', 'createdAt': '2026-11-13T05:03:20.060Z',
                   'lastModifiedAt': '2026-11-13T05:03:31.516Z', 'agentMode': 'release-writer',
                   'modelId': 'auto', 'memoryConfig': {'enabled': True}, 'status': ''}
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('session.json', json.dumps(session))
            archive.writestr('messages.jsonl', '')
        with tempfile.TemporaryDirectory() as root:
            store = InMemorySessionStore()
            with patch('kiro_agent_sdk.archive.export_session_archive', return_value=output.getvalue()):
                await import_session_to_store('source', store, directory=root)
            listed = (await list_sessions_from_store(store, directory=root))[0]
            self.assertEqual({key: value for key, value in listed.items() if key != 'mtime'}, {
                'sessionId': 'source', 'title': 'Kiro Generated Title', 'createdAt': '2026-11-13T05:03:20.060Z',
                'lastModifiedAt': '2026-11-13T05:03:31.516Z', 'agentMode': 'release-writer', 'modelId': 'auto'})
            self.assertEqual((listed.agent, listed.model, listed.status), ('release-writer', 'auto', None))

    async def test_import_existing_session_preserves_archive_and_text(self):
        store = InMemorySessionStore()
        archive = transcript_archive()
        with patch('kiro_agent_sdk.archive.export_session_archive', return_value=archive):
            await import_session_to_store('source', store)
            with self.assertRaises(FileExistsError):
                await import_session_to_store('source', store)
        messages = await get_session_messages_from_store(store, 'source')
        self.assertEqual([role(item) for item in messages], ['user', 'agent'])
        self.assertEqual([item.message_id for item in messages], ['user-1', 'assistant-1'])
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': 'source'}
        entries = await store.load(key)
        self.assertEqual(sum(entry['type'] == 'archive_message' for entry in entries), 3)
        self.assertEqual(entries[-1]['type'], 'session_archive')
        with patch('kiro_agent_sdk.archive._run_cli',
                   return_value=b'{\"kind\":\"importSession\",\"data\":{\"path\":\"/kiro/cli_new\"}}'):
            self.assertEqual(await restore_session_from_store(store, 'source'), 'cli_new')

    async def test_concurrent_import_has_one_winner_for_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'store.db'
            stores = [SQLiteSessionStore(path), SQLiteSessionStore(path)]
            started, both = 0, asyncio.Event()
            async def export(*args, **kwargs):
                nonlocal started
                started += 1
                if started == 2:
                    both.set()
                await both.wait()
                return transcript_archive()
            with patch('kiro_agent_sdk.archive.export_session_archive', side_effect=export):
                results = await asyncio.gather(*(import_session_to_store('source', store)
                                                 for store in stores), return_exceptions=True)
            self.assertEqual(sum(item is None for item in results), 1)
            self.assertEqual(sum(isinstance(item, FileExistsError) for item in results), 1)
            entries = await stores[0].load({'project_key': project_key_for_directory(os.getcwd()),
                                            'session_id': 'source'})
            self.assertEqual(sum(entry['type'] == 'session_archive' for entry in entries), 1)

    async def test_import_rejects_invalid_or_excessive_messages_without_writes(self):
        store = InMemorySessionStore()
        with patch('kiro_agent_sdk.archive.export_session_archive', return_value=transcript_archive()):
            with self.assertRaisesRegex(SessionArchiveError, 'message count'):
                await import_session_to_store('source', store, max_messages=1)
            with self.assertRaises(ValueError):
                await import_session_to_store('source', store, include_subagents='no')
            with self.assertRaises(ValueError):
                await import_session_to_store('source', store, batch_size=0)
        with patch('kiro_agent_sdk.archive.export_session_archive', return_value=archive_bytes()):
            with self.assertRaisesRegex(SessionArchiveError, 'Unexpected Kiro archive message'):
                await import_session_to_store('source', store)
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': 'source'}
        self.assertIsNone(await store.load(key))

    async def test_import_can_exclude_subagent_transcripts_on_tested_cli(self):
        store = InMemorySessionStore()
        archive = transcript_archive(with_subagent=True)
        with patch('kiro_agent_sdk.archive.export_session_archive', return_value=archive):
            await import_session_to_store('source', store, include_subagents=False, cli_path=FAKE)
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': 'source'}
        entries = await store.load(key)
        snapshot = base64.b64decode(entries[-1]['data']['base64'])
        with zipfile.ZipFile(io.BytesIO(snapshot)) as restored:
            self.assertIn('messages.jsonl', restored.namelist())
            self.assertNotIn('sub-executions/child-1.jsonl', restored.namelist())
        self.assertEqual([role(message) for message in await get_session_messages_from_store(store, 'source')],
                         ['user', 'agent'])
        other = InMemorySessionStore()
        with patch('kiro_agent_sdk.archive.export_session_archive', return_value=archive) as export:
            with self.assertRaisesRegex(SessionArchiveError, 'tested Kiro CLI'):
                await import_session_to_store('source', other, include_subagents=False, cli_path=FAKE,
                                              env={'FAKE_VERSION': 'kiro-cli 9.0'})
            export.assert_not_awaited()
        self.assertIsNone(await other.load(key))

    async def test_custom_store_receives_bounded_batches_and_partial_failure_is_visible(self):
        class Store:
            def __init__(self, fail_at=None):
                self.entries = None
                self.batch_sizes = []
                self.fail_at = fail_at
            async def load(self, key):
                return self.entries
            async def append(self, key, entries):
                self.batch_sizes.append(len(entries))
                if len(self.batch_sizes) == self.fail_at:
                    raise RuntimeError('write failed')
                if self.entries is None:
                    self.entries = []
                self.entries.extend(entries)
        with patch('kiro_agent_sdk.archive.export_session_archive', return_value=transcript_archive()):
            store = Store()
            await import_session_to_store('source', store, batch_size=2)
            self.assertTrue(all(size <= 2 for size in store.batch_sizes))
            self.assertEqual(store.entries[-1]['type'], 'session_archive')
            partial = Store(fail_at=2)
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                await import_session_to_store('source', partial, batch_size=1)
            self.assertIsNotNone(partial.entries)
            self.assertFalse(any(entry['type'] == 'session_archive' for entry in partial.entries))
            with self.assertRaises(SessionArchiveError):
                await restore_session_from_store(partial, 'source')

    async def test_export_import_and_store_recovery(self):
        original = archive_bytes()
        calls = []
        async def helper(arguments, cwd, cli_path, env):
            calls.append((arguments, cwd, env))
            if arguments[1] == 'export-session':
                Path(arguments[arguments.index('--out') + 1]).write_bytes(original)
                return json.dumps({'kind': 'exportSession', 'data': {'path': 'unused'}}).encode()
            imported = Path(arguments[arguments.index('--archive') + 1]).read_bytes()
            self.assertEqual(imported, original)
            return json.dumps({'kind': 'importSession', 'data': {'path': '/kiro/cli_new'}}).encode()

        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as destination:
            store = InMemorySessionStore()
            with patch('kiro_agent_sdk.archive._run_cli', side_effect=helper):
                saved = await export_session_archive('source', cwd=source, env={'CUSTOM': 'value'})
                self.assertEqual(saved, original)
                self.assertEqual(await import_session_archive(saved, cwd=destination), 'cli_new')
                key = {'project_key': project_key_for_directory(source), 'session_id': 'source'}
                await store.append(key, [{'type': 'session_archive', 'data': {
                    'format': ARCHIVE_FORMAT, 'session_id': 'source', 'size': len(saved),
                    'sha256': hashlib.sha256(saved).hexdigest(), 'base64': base64.b64encode(saved).decode(),
                }}])
                restored = await restore_session_from_store(store, 'source', source, target_directory=destination)
                self.assertEqual(restored, 'cli_new')
            self.assertEqual(calls[0][2], {'CUSTOM': 'value'})
            self.assertEqual(calls[-1][1], destination)

    async def test_rejects_unsafe_or_oversized_archives_before_import(self):
        corrupt = bytearray(archive_bytes())
        with zipfile.ZipFile(io.BytesIO(corrupt)) as zipped:
            info = zipped.getinfo('session.json')
            offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
            corrupt[offset] ^= 16
        bad_archives = [b'not a zip', bytes(corrupt), archive_bytes(('../escape', 'oops')),
                        archive_bytes(('C:/escape', 'oops')), archive_bytes(('a\\escape', 'oops'))]
        symlink = zipfile.ZipInfo('link')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        bad_archives.append(archive_bytes((symlink, '/outside')))
        with patch('kiro_agent_sdk.archive._run_cli') as helper:
            for bad in bad_archives:
                with self.subTest(size=len(bad)), self.assertRaises(SessionArchiveError):
                    await import_session_archive(bad)
            with self.assertRaises(SessionArchiveError):
                await import_session_archive(archive_bytes(), max_bytes=8)
            with self.assertRaises(SessionArchiveError):
                await import_session_archive(archive_bytes(), max_uncompressed_bytes=1)
            helper.assert_not_called()

    async def test_corrupt_or_missing_mirror_archive_is_rejected(self):
        store = InMemorySessionStore()
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': 'source'}
        with self.assertRaises(SessionArchiveError):
            await restore_session_from_store(store, 'source')
        await store.append(key, [{'type': 'session_archive', 'data': {
            'format': ARCHIVE_FORMAT, 'session_id': 'source', 'base64': base64.b64encode(archive_bytes()).decode(),
            'size': len(archive_bytes()), 'sha256': 'incorrect',
        }}])
        with patch('kiro_agent_sdk.archive._run_cli') as helper:
            with self.assertRaisesRegex(SessionArchiveError, 'integrity'):
                await restore_session_from_store(store, 'source')
            helper.assert_not_called()

    async def test_new_activity_requires_explicit_stale_restore(self):
        data = archive_bytes()
        store = InMemorySessionStore()
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': 'source'}
        await store.append(key, [{'type': 'session_archive', 'data': {
            'format': ARCHIVE_FORMAT, 'session_id': 'source', 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'base64': base64.b64encode(data).decode(),
        }}, {'type': 'prompt', 'data': [{'type': 'text', 'text': 'newer work'}]}])
        with patch('kiro_agent_sdk.archive._run_cli',
                   return_value=b'{\"kind\":\"importSession\",\"data\":{\"path\":\"/kiro/cli_new\"}}') as helper:
            with self.assertRaisesRegex(SessionArchiveError, 'activity after'):
                await restore_session_from_store(store, 'source')
            helper.assert_not_called()
            self.assertEqual(await restore_session_from_store(store, 'source', allow_stale=True), 'cli_new')

    async def test_client_mirrors_archive_and_handles_timeout(self):
        archive = archive_bytes()
        store = InMemorySessionStore()
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', session_store=store, session_store_archive=True)
        with patch('kiro_agent_sdk.archive.export_session_archive', return_value=archive):
            async with KiroSDKClient(options) as client:
                session_id = client.session_id
                await client.query('hello')
                _ = [message async for message in client.receive_response()]
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': session_id}
        entries = await store.load(key)
        snapshot = next(entry['data'] for entry in entries if entry['type'] == 'session_archive')
        self.assertEqual(base64.b64decode(snapshot['base64']), archive)
        self.assertEqual(snapshot['sha256'], hashlib.sha256(archive).hexdigest())
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        options.session_store_archive_timeout = 0.05
        client = KiroSDKClient(options)
        with patch('kiro_agent_sdk.archive.export_session_archive', side_effect=hang):
            await client.connect()
            with self.assertRaisesRegex(SessionArchiveError, 'Timed out'):
                await client.query('hello')
                _ = [message async for message in client.receive_response()]
            self.assertFalse(client._connected)

    async def test_archive_requires_local_v3_and_a_store(self):
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(session_store_archive=True))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', session_store_archive=True,
                                           session_store=InMemorySessionStore(), session_store_archive_max_bytes=0))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', session_store_archive=True,
                                           session_store=InMemorySessionStore()), transport=object())
        for bad_id in ('../outside', '--help', 'bad\\\\name', ''):
            with self.subTest(session_id=bad_id), self.assertRaises(ValueError):
                await export_session_archive(bad_id)

    async def test_archive_helper_timeout(self):
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        with patch('kiro_agent_sdk.archive._run_cli', side_effect=hang):
            with self.assertRaisesRegex(SessionArchiveError, 'Timed out'):
                await import_session_archive(archive_bytes(), timeout=0.05)
        with patch('kiro_agent_sdk.archive._run_cli', side_effect=ProcessError('unsupported')):
            with self.assertRaisesRegex(SessionArchiveError, 'helper failed'):
                await import_session_archive(archive_bytes())

    async def test_client_can_resume_from_archive_store(self):
        store = InMemorySessionStore()
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            opts = KiroAgentOptions(cli_path=FAKE, cwd=target, agent_engine='v3',
                                    resume='source-id', session_store=store, resume_from_store=True,
                                    session_store_source_directory=source)
            with patch('kiro_agent_sdk.archive.restore_session_from_store', return_value='restored-id') as restore:
                async with KiroSDKClient(opts) as client:
                    self.assertEqual(client.session_id, 'restored-id')
                    self.assertEqual(client.restored_session_id, 'restored-id')
                    self.assertEqual(restore.await_args.args[:3], (store, 'source-id', source))
                    self.assertEqual(restore.await_args.kwargs['target_directory'], target)
                    await client.disconnect()
                    await client.connect()
                    self.assertEqual(client.session_id, 'restored-id')
                    self.assertEqual(restore.await_count, 1)

    async def test_restored_id_is_available_after_load_failure(self):
        opts = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', resume='source-id',
                                session_store=InMemorySessionStore(), resume_from_store=True,
                                env={'FAKE_LOAD_FAIL': '1'})
        client = KiroSDKClient(opts)
        with patch('kiro_agent_sdk.archive.restore_session_from_store', return_value='restored-id'):
            with self.assertRaisesRegex(ProtocolError, 'load failed'):
                await client.connect()
        self.assertEqual(client.restored_session_id, 'restored-id')
        self.assertFalse(client._connected)

    async def test_resume_from_store_requires_explicit_local_v3_options(self):
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(resume_from_store=True))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(session_store_source_directory='/tmp'))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v2', resume='source-id',
                                           session_store=InMemorySessionStore(), resume_from_store=True))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', resume='source-id',
                                           session_store=InMemorySessionStore(), resume_from_store=True,
                                           fork_session=True))
