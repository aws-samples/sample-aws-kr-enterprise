import io
import json
import os
import unittest
import zipfile
from unittest.mock import patch

from kiro_agent_sdk import (InMemorySessionStore, SessionArchiveError, get_subagent_history,
                            get_subagent_history_from_store, get_subagent_messages,
                            get_subagent_messages_from_store, list_subagents, list_subagents_from_store,
                            project_key_for_directory)
from kiro_agent_sdk.archive import _archive_payload
from history_helpers import blocks, role


def subagent_archive():
    parent = [
        {'id': 'tool-1-sub-agent-start', 'payload': {'type': 'sub_agent_start', 'subSessionId': 'child-1'}},
        {'id': 'tool-1-sub-agent-complete', 'payload': {'type': 'sub_agent_complete', 'subSessionId': 'child-1'}},
    ]
    child = [
        {'id': 'thinking', 'payload': {'type': 'assistant', 'operationType': 'Reasoning', 'content': 'private'}},
        {'id': 'answer', 'payload': {'type': 'assistant', 'operationType': 'Say', 'content': 'CHILD_OK'}},
        {'id': 'tool', 'payload': {'type': 'tool_call', 'input': {'path': 'example.txt'}}},
    ]
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('session.json', '{}')
        archive.writestr('messages.jsonl', '\n'.join(json.dumps(e) for e in parent) + '\n')
        archive.writestr('sub-executions/child-1.jsonl', '\n'.join(json.dumps(e) for e in child) + '\n')
        archive.writestr('sub-executions/workflow-1.jsonl', json.dumps(child[-1]) + '\n')
    return output.getvalue()


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_subagent_list_and_text_preview(self):
        data = subagent_archive()
        with patch('kiro_agent_sdk.subagents.export_session_archive', return_value=data):
            self.assertEqual(await list_subagents('session'), ['child-1'])
            messages = await get_subagent_messages('session', 'child-1')
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].message_id, 'answer')
            self.assertEqual(messages[0].parent_tool_call_id, 'tool-1')
            self.assertEqual(messages[0].content[0].text, 'CHILD_OK')
            self.assertEqual(await get_subagent_messages('session', 'child-1', limit=0), [])
            self.assertEqual(await get_subagent_messages('session', 'workflow-1'), [])
            raw = await get_subagent_history('session', 'child-1', limit=2, offset=1)
            self.assertEqual([entry['id'] for entry in raw], ['answer', 'tool'])
            self.assertEqual(raw[1]['payload']['input']['path'], 'example.txt')
            self.assertEqual(await get_subagent_history('session', 'workflow-1'), [])
            with self.assertRaises(ValueError):
                await get_subagent_messages('session', '../unsafe')
            with self.assertRaises(SessionArchiveError):
                await list_subagents('session', max_messages=1)

    async def test_event_only_mirror_lists_observed_subagents(self):
        store = InMemorySessionStore()
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': 'session'}
        def update(kind, agent_id):
            return {'type': 'update', 'data': {'sessionUpdate': kind, 'toolCallId': 'tool-1',
                    '_meta': {'kiro': {'kind': 'agent-subtask', 'agentSubtaskId': agent_id}}}}
        await store.append(key, [update('tool_call', 'provisional'), update('tool_call_update', 'child-1')])
        self.assertEqual(await list_subagents_from_store(store, 'session'), ['child-1'])
        with self.assertRaisesRegex(SessionArchiveError, 'require'):
            await get_subagent_messages_from_store(store, 'session', 'child-1')
        with self.assertRaisesRegex(SessionArchiveError, 'require'):
            await get_subagent_history_from_store(store, 'session', 'child-1')
        self.assertEqual(await list_subagents_from_store(store, 'missing'), [])

    async def test_mirror_archive_and_newer_events_are_not_confused(self):
        store = InMemorySessionStore()
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': 'session'}
        await store.append(key, [{'type': 'session_archive', 'data': _archive_payload('session', subagent_archive())}])
        initial = await get_subagent_messages_from_store(store, 'session', 'child-1')
        self.assertEqual(initial[0].parent_tool_call_id, 'tool-1')
        raw = await get_subagent_history_from_store(store, 'session', 'child-1')
        self.assertEqual([entry['id'] for entry in raw], ['thinking', 'answer', 'tool'])
        await store.append(key, [{'type': 'prompt', 'data': []}, {'type': 'update', 'data': {
            'sessionUpdate': 'tool_call', 'toolCallId': 'tool-2',
            '_meta': {'kiro': {'kind': 'agent-subtask', 'agentSubtaskId': 'child-2'}}}}])
        self.assertEqual(await list_subagents_from_store(store, 'session'), ['child-1', 'child-2'])
        with self.assertRaisesRegex(SessionArchiveError, 'activity after'):
            await get_subagent_messages_from_store(store, 'session', 'child-1')
        with self.assertRaisesRegex(SessionArchiveError, 'activity after'):
            await get_subagent_history_from_store(store, 'session', 'child-1')
        old = await get_subagent_messages_from_store(store, 'session', 'child-1', allow_stale=True)
        self.assertEqual(old[0].content[0].text, 'CHILD_OK')
        raw = await get_subagent_history_from_store(store, 'session', 'child-1', allow_stale=True, limit=1)
        self.assertEqual(raw[0]['id'], 'thinking')

    async def test_corrupt_mirror_archive_is_not_silently_ignored(self):
        store = InMemorySessionStore()
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': 'session'}
        data = _archive_payload('session', subagent_archive())
        data['sha256'] = 'incorrect'
        await store.append(key, [{'type': 'session_archive', 'data': data}])
        with self.assertRaisesRegex(SessionArchiveError, 'integrity'):
            await list_subagents_from_store(store, 'session')
