import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Annotated, TypedDict

import kiro_agent_sdk.mcp as mcp_module
from kiro_agent_sdk import (AgentDefinition, AgentMessage, CheckpointRecoveryError, CLIConnectionError, HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError, ToolAnnotations,
                            ResultError, create_mcp_server, tool, list_sessions, list_models, get_session_info,
                            get_session_history, get_session_messages, InMemorySessionStore, project_key_for_directory,
                            get_session_info_from_store, get_session_messages_from_store, list_sessions_from_store,
                            delete_session, delete_session_via_store, rename_session_via_store)
from kiro_agent_sdk.mcp import SdkMcpBridge, _schema
from kiro_agent_sdk.hooks import NativeHookBridge
from kiro_agent_sdk.session_store import MirrorWriter
from history_helpers import blocks, role

FAKE = os.path.join(os.path.dirname(__file__), 'fake_kiro.py')

class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_shim_exits_cleanly_when_peer_closes_with_stdin_open(self):
        received = asyncio.Event()
        async def peer(reader, writer):
            try:
                self.assertEqual(await reader.readline(), b"test-token\n")
                self.assertEqual(await reader.readline(), b"probe\n")
                received.set()
                await asyncio.sleep(0.05)
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        helper = Path(mcp_module.__file__).with_name("_mcp_bridge.py")
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(helper), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "KIRO_SDK_MCP_PORT": str(port), "KIRO_SDK_MCP_TOKEN": "test-token"})
        try:
            process.stdin.write(b"probe\n")
            await process.stdin.drain()
            await asyncio.wait_for(received.wait(), 3)
            self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
            self.assertEqual(await process.stderr.read(), b"")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            process.stdin.close()
            server.close()
            await server.wait_closed()

    async def test_bridge_close_closes_open_connection_without_waiting_for_peer(self):
        bridge = SdkMcpBridge(create_mcp_server('test', tools=[]))
        config = await bridge.start()
        env = {entry['name']: entry['value'] for entry in config['env']}
        reader, writer = await asyncio.open_connection('127.0.0.1', int(env['KIRO_SDK_MCP_PORT']))
        writer.write((env['KIRO_SDK_MCP_TOKEN'] + '\n').encode())
        await writer.drain()
        for _ in range(50):
            if bridge.connections:
                break
            await asyncio.sleep(0.01)
        try:
            await asyncio.wait_for(bridge.close(), 2)
            try:
                self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
            except ConnectionResetError:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
            await bridge.close()

    async def test_sdk_tool_suspension_cancels_inflight_and_allows_resume(self):
        started, released = asyncio.Event(), asyncio.Event()

        @tool('slow', 'Wait', {})
        async def slow(args):
            started.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                await released.wait()
            return {'content': [{'type': 'text', 'text': 'late'}]}

        bridge = SdkMcpBridge(create_mcp_server('test', tools=[slow]))
        config = await bridge.start()
        env = {entry['name']: entry['value'] for entry in config['env']}
        reader, writer = await asyncio.open_connection('127.0.0.1', int(env['KIRO_SDK_MCP_PORT']))
        writer.write((env['KIRO_SDK_MCP_TOKEN'] + '\n').encode())

        async def send(message):
            writer.write((json.dumps(message) + '\n').encode())
            await writer.drain()

        try:
            await send({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'slow', 'arguments': {}}})
            await asyncio.wait_for(started.wait(), 2)
            bridge.suspend_tools()
            await bridge.cancel_tools()
            response = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual((response['id'], response['error']['code']), (1, -32800))
            await send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                        'params': {'name': 'slow', 'arguments': {}}})
            response = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual((response['id'], response['error']['code']), (2, -32800))
            released.set()
            bridge.resume_tools()
            await send({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                        'params': {'name': 'slow', 'arguments': {}}})
            response = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual((response['id'], response['result']['content'][0]['text']), (3, 'late'))
        finally:
            released.set()
            writer.close()
            await writer.wait_closed()
            await bridge.close()

    async def test_cancelled_mcp_tool_cannot_reply_late_after_id_reuse(self):
        started, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        @tool('slow', 'Wait', {})
        async def slow(args):
            started.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                await released.wait()
            finished.set()
            return {'content': [{'type': 'text', 'text': 'late'}]}

        bridge = SdkMcpBridge(create_mcp_server('test', tools=[slow]))
        config = await bridge.start()
        env = {entry['name']: entry['value'] for entry in config['env']}
        reader, writer = await asyncio.open_connection('127.0.0.1', int(env['KIRO_SDK_MCP_PORT']))
        writer.write((env['KIRO_SDK_MCP_TOKEN'] + '\n').encode())

        async def send(message):
            writer.write((json.dumps(message) + '\n').encode())
            await writer.drain()

        try:
            await send({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'slow', 'arguments': {}}})
            await asyncio.wait_for(started.wait(), 2)
            await send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 1}})
            cancelled = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual((cancelled['id'], cancelled['error']['code']), (1, -32800))
            await send({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
            reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual((reply['id'], reply['result']), (1, {}))
            released.set()
            await asyncio.wait_for(finished.wait(), 2)
            await send({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
            reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual((reply['id'], reply['result']), (2, {}))
        finally:
            released.set()
            writer.close()
            await writer.wait_closed()
            await bridge.close()

    async def test_concurrent_mcp_requests_and_cancellation(self):
        import asyncio
        started, finished = asyncio.Event(), asyncio.Event()
        @tool('slow', 'Wait', {})
        async def slow(args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        bridge = SdkMcpBridge(create_mcp_server('test', tools=[slow]))
        config = await bridge.start()
        env = {entry['name']: entry['value'] for entry in config['env']}
        reader, writer = await asyncio.open_connection('127.0.0.1', int(env['KIRO_SDK_MCP_PORT']))
        writer.write((env['KIRO_SDK_MCP_TOKEN'] + '\n').encode())
        async def send(message):
            writer.write((json.dumps(message) + '\n').encode())
            await writer.drain()
        try:
            await send({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                        'params': {'name': 'slow', 'arguments': {}}})
            await asyncio.wait_for(started.wait(), 2)
            await send({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
            response = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual(response['id'], 2)
            await send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                        'params': {'requestId': 1}})
            response = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual(response['error']['code'], -32800)
            await asyncio.wait_for(finished.wait(), 2)
            request = {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                       'params': {'name': 'slow', 'arguments': {}}}
            cancel = {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 3}}
            writer.write((json.dumps(request) + '\n' + json.dumps(cancel) + '\n').encode())
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertEqual((response['id'], response['error']['code']), (3, -32800))
        finally:
            writer.close()
            await writer.wait_closed()
            await bridge.close()

    async def test_sdk_tool_annotations(self):
        hints = ToolAnnotations(read_only_hint=True, destructiveHint=False)
        self.assertTrue(hints.readOnlyHint)
        self.assertEqual(dict(hints), {'readOnlyHint': True, 'destructiveHint': False})
        with self.assertRaises(ValueError):
            ToolAnnotations(readOnlyHint="yes")

    async def test_schema_mapping_and_typeddict(self):
        class Input(TypedDict, total=False):
            name: Annotated[str, 'Display name']
            values: list[int]
        schema = _schema(Input)
        self.assertEqual(schema['required'], [])
        self.assertEqual(schema['properties']['name']['description'], 'Display name')
        self.assertEqual(schema['properties']['values']['items']['type'], 'integer')
        self.assertEqual(_schema({'type': str})['properties']['type']['type'], 'string')
        self.assertEqual(_schema({'anyOf': [{'type': 'string'}]})['anyOf'][0]['type'], 'string')

    async def test_in_process_tool_and_validation(self):
        calls = []
        @tool('add', 'Add values', {'x': int})
        async def add(args):
            calls.append(args)
            return {'content': [{'type': 'text', 'text': str(args['x'] + 1)}]}
        bridge = SdkMcpBridge(create_mcp_server('test', tools=[add]))
        config = await bridge.start()
        env = {entry['name']: entry['value'] for entry in config['env']}
        import asyncio
        process = await asyncio.create_subprocess_exec(config['command'], *config['args'],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env={**os.environ, **env})
        async def request(ident, method, params):
            process.stdin.write((json.dumps({'jsonrpc':'2.0','id':ident,'method':method,'params':params})+'\n').encode())
            await process.stdin.drain()
            return json.loads(await asyncio.wait_for(process.stdout.readline(), 3))['result']
        try:
            hello = await request(1, 'initialize', {'protocolVersion':'2025-03-26'})
            self.assertEqual(hello['protocolVersion'], '2025-03-26')
            self.assertEqual((await request(2, 'tools/list', {}))['tools'][0]['name'], 'add')
            result = await request(3, 'tools/call', {'name':'add','arguments':{'x':2}})
            self.assertEqual(result['content'][0]['text'], '3')
            bad = await request(4, 'tools/call', {'name':'add','arguments':{'x':'bad'}})
            self.assertTrue(bad['isError'])
            self.assertEqual(calls, [{'x':2}])
        finally:
            process.stdin.close()
            await process.wait()
            await bridge.close()

class HookTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_hook_removal_aborts_active_session(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', env={'HOME': root},
                                       native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher()]})
            client = KiroSDKClient(options)
            await client.connect()
            process = client._process
            with self.assertRaisesRegex(ProtocolError, 'native hooks stopped'):
                await client.query('disable_native_hook')
                _ = [message async for message in client.receive_response()]
            await asyncio.wait_for(process.wait(), 3)
            self.assertFalse(client._connected)

    async def test_native_hook_change_during_initialization_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3',
                                       env={'HOME': root, 'FAKE_HOOK_DISABLE_AT_START': '1'},
                                       native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher()]})
            client = KiroSDKClient(options)
            with self.assertRaisesRegex(ProtocolError, 'did not register'):
                await client.connect()
            self.assertFalse(client._connected)

    async def test_valid_native_hook_changes_remain_usable(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3',
                                       env={'HOME': root, 'FAKE_HOOK_VALID_AT_START': '1'},
                                       native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher()]})
            async with KiroSDKClient(options) as client:
                await client.query('valid_native_hook')
                messages = [message async for message in client.receive_response()]
                self.assertEqual(messages[-1].result, 'valid')

    async def test_rewind_preserves_a_conversation_fork(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3')
        async with KiroSDKClient(options) as client:
            result = await client.rewind_files_preserving_conversation('user-1')
            self.assertEqual(result['original_session_id'], 'test-session')
            self.assertEqual(result['fork_session_id'], 'forked')
            self.assertTrue(result['checkpoint_result']['success'])
            self.assertEqual(client.last_rewind_fork_id, 'forked')
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', env={'FAKE_CHECKPOINT_FAILURE': '1'})
        async with KiroSDKClient(options) as client:
            with self.assertRaises(CheckpointRecoveryError) as raised:
                await client.rewind_files_preserving_conversation('user-1')
            self.assertEqual(raised.exception.fork_session_id, 'forked')
            self.assertFalse(raised.exception.checkpoint_result['success'])
            self.assertEqual(client.last_rewind_fork_id, 'forked')
            with self.assertRaisesRegex(CLIConnectionError, 'Checkpoint outcome is uncertain'):
                await client.query('do not continue')
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', env={'FAKE_FORK_SAME': '1'})
        async with KiroSDKClient(options) as client:
            with self.assertRaisesRegex(ProtocolError, 'distinct conversation fork'):
                await client.rewind_files_preserving_conversation('user-1')
            self.assertIsNone(client.last_rewind_fork_id)

    async def test_cancelled_rewind_retains_fork_and_blocks_new_turns(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', env={'FAKE_CHECKPOINT_HANG': '1'})
        async with KiroSDKClient(options) as client:
            task = asyncio.create_task(client.rewind_files_preserving_conversation('user-1'))
            async def wait_for_fork():
                while client.last_rewind_fork_id is None:
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_for_fork(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(client.last_rewind_fork_id, 'forked')
            with self.assertRaisesRegex(CLIConnectionError, 'Checkpoint outcome is uncertain'):
                await client.query('do not continue')

    async def test_mirror_metadata_listing_loads_only_selected_page(self):
        class CountingStore(InMemorySessionStore):
            def __init__(self):
                super().__init__()
                self.loaded = []
            async def list_session_info(self, project_key):
                raise NotImplementedError
            async def load(self, key):
                self.loaded.append(key['session_id'])
                return await super().load(key)
        store = CountingStore()
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': 'first'}
        await store.append(key, [{'type': 'prompt', 'data': []}])
        await store.append({**key, 'session_id': 'second'}, [{'type': 'prompt', 'data': []}])
        page = await list_sessions_from_store(store, limit=1)
        self.assertEqual(store.loaded, [page[0].session_id])

    async def test_mirror_info_without_optional_listing(self):
        class BasicStore(InMemorySessionStore):
            async def list_sessions(self, project_key):
                raise NotImplementedError
        store = BasicStore()
        await rename_session_via_store(store, 'test-session', 'Title')
        self.assertEqual((await get_session_info_from_store(store, 'test-session')).title, 'Title')
        self.assertEqual((await list_sessions_from_store(store))[0].title, 'Title')

        class NoListingStore(BasicStore):
            async def list_session_info(self, project_key):
                raise NotImplementedError

        with self.assertRaises(ProtocolError):
            await list_sessions_from_store(NoListingStore())

    async def test_mirror_writer_batches_and_flushes_in_order(self):
        class Store:
            def __init__(self):
                self.batches = []
            async def append(self, key, entries):
                self.batches.append(list(entries))
        store = Store()
        async def on_error(error):
            raise AssertionError(error)
        writer = MirrorWriter(store, {'project_key': 'p', 'session_id': 's'}, 'batched', on_error)
        await writer.append({'type': 'first'})
        await writer.append({'type': 'second'})
        await writer.flush()
        self.assertEqual([[entry['type'] for entry in batch] for batch in store.batches],
                         [['first', 'second']])
        self.assertEqual(writer._pending_bytes, 0)
        await writer.close()

    async def test_slow_eager_mirror_does_not_block_streaming(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Store:
            async def append(self, key, entries):
                started.set()
                await release.wait()
        options = KiroAgentOptions(cli_path=FAKE, session_store=Store(), session_store_flush='eager')
        async with KiroSDKClient(options) as client:
            await client.query('hello')
            await asyncio.wait_for(started.wait(), 2)
            responses = client.receive_response()
            try:
                async def first_assistant():
                    async for message in responses:
                        if isinstance(message, AgentMessage):
                            return message
                message = await asyncio.wait_for(first_assistant(), 2)
                self.assertEqual(message.content[0].text, 'hello')
            finally:
                release.set()
            remaining = [message async for message in responses]
            self.assertEqual(remaining[-1].result, 'hello')

    async def test_mirror_flush_timeout_is_reported(self):
        cancelled = asyncio.Event()
        class Store:
            async def append(self, key, entries):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        options = KiroAgentOptions(cli_path=FAKE, session_store=Store(), session_store_flush='eager',
                                   session_store_flush_timeout=0.1)
        client = KiroSDKClient(options)
        await client.connect()
        with self.assertRaisesRegex(CLIConnectionError, 'session mirror'):
            await client.query('hello')
            _ = [message async for message in client.receive_response()]
        self.assertFalse(client._connected)
        await asyncio.wait_for(cancelled.wait(), 2)

    async def test_mirror_timeout_does_not_wait_forever_for_ignored_cancellation(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Store:
            async def append(self, key, entries):
                started.set()
                if release.is_set():
                    return
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
        options = KiroAgentOptions(cli_path=FAKE, session_store=Store(), session_store_flush='eager',
                                   session_store_flush_timeout=0.1)
        client = KiroSDKClient(options)
        await client.connect()
        writer = client._mirror_writer
        try:
            await client.query('hello')
            await asyncio.wait_for(started.wait(), 2)
            with self.assertRaises(CLIConnectionError):
                await asyncio.wait_for(
                    self._consume_response(client), 4)
        finally:
            release.set()
            done, _ = await asyncio.wait((writer._task,), timeout=3)
            self.assertIn(writer._task, done)

    async def _consume_response(self, client):
        return [message async for message in client.receive_response()]

    async def test_session_mirror(self):
        store = InMemorySessionStore()
        options = KiroAgentOptions(cli_path=FAKE, session_store=store)
        async with KiroSDKClient(options) as client:
            session_id = client.session_id
            await client.query('hello')
            _ = [message async for message in client.receive_response()]
        key = {'project_key': project_key_for_directory(os.getcwd()), 'session_id': session_id}
        entries = await store.load(key)
        self.assertEqual(entries[0]['type'], 'prompt')
        self.assertEqual([entry['type'] for entry in entries][-2:], ['result', 'session_info'])
        self.assertEqual(entries[-2]['data']['result'], 'hello')
        entries[0]['data'][0]['text'] = 'changed'
        self.assertEqual((await store.load(key))[0]['data'][0]['text'], 'hello')
        self.assertEqual((await store.list_sessions(key['project_key']))[0]['session_id'], session_id)
        listed = (await list_sessions_from_store(store))[0]
        self.assertEqual((listed.session_id, listed.title), (session_id, 'Fake session'))
        self.assertEqual(await get_session_info_from_store(store, session_id), listed)
        messages = await get_session_messages_from_store(store, session_id)
        self.assertEqual(role(messages[0]), 'user')
        self.assertEqual(''.join(m.content[0].text for m in messages if role(m) == "agent"), 'hello')
        await delete_session_via_store(store, session_id)
        self.assertIsNone(await store.load(key))

    async def test_mirror_failure_surfaces(self):
        class FailingStore:
            async def append(self, key, entries):
                if any(entry['type'] == 'update' for entry in entries):
                    raise RuntimeError('store unavailable')
        options = KiroAgentOptions(cli_path=FAKE, session_store=FailingStore())
        async with KiroSDKClient(options) as client:
            with self.assertRaisesRegex(RuntimeError, 'store unavailable'):
                await client.query('hello')
                _ = [message async for message in client.receive_response()]

    async def test_native_hook_bridge(self):
        import asyncio
        async def block(data, ident, context):
            self.assertEqual(data['tool_name'], 'read')
            return {'block': 'no read'}
        bridge = NativeHookBridge({'PreToolUse': [HookMatcher(hooks=[block])]})
        _, env = await bridge.start()
        path = os.path.join(os.path.dirname(__file__), '..', 'src', 'kiro_agent_sdk', '_hook_bridge.py')
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, path, env={**os.environ, **env},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await process.communicate(json.dumps({'hook_event_name': 'preToolUse', 'tool_name': 'read'}).encode())
            self.assertEqual(process.returncode, 2)
            self.assertIn(b'no read', stderr)
        finally:
            await bridge.close()

    async def test_native_post_hook_is_observational(self):
        import asyncio
        seen = []
        async def observe(data, ident, context):
            seen.append(data['tool_name'])
            return {}
        bridge = NativeHookBridge({'PostToolUse': [HookMatcher(hooks=[observe])]})
        _, env = await bridge.start()
        path = os.path.join(os.path.dirname(__file__), '..', 'src', 'kiro_agent_sdk', '_hook_bridge.py')
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, path, env={**os.environ, **env},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await process.communicate(json.dumps({'hook_event_name': 'PostToolUse', 'tool_name': 'read'}).encode())
            self.assertEqual(process.returncode, 0)
            self.assertEqual(seen, ['read'])
        finally:
            await bridge.close()

    async def test_native_file_hook_matches_path(self):
        import asyncio
        seen = []
        async def observe(data, ident, context):
            seen.append(data['file_path'])
            return {}
        bridge = NativeHookBridge({'PostFileSave': [HookMatcher(matcher=r'\.py$', hooks=[observe])]})
        _, env = await bridge.start()
        path = os.path.join(os.path.dirname(__file__), '..', 'src', 'kiro_agent_sdk', '_hook_bridge.py')
        try:
            for file_path in ('module.py', 'notes.txt'):
                process = await asyncio.create_subprocess_exec(sys.executable, path, env={**os.environ, **env},
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await process.communicate(json.dumps({'hook_event_name': 'PostFileSave', 'file_path': file_path}).encode())
                self.assertEqual(process.returncode, 0)
            self.assertEqual(seen, ['module.py'])
        finally:
            await bridge.close()
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(native_hook_bridge_mode="same-user", native_hooks={'fileEdited': [HookMatcher()]}))

    async def test_native_stop_hook_requests_continuation(self):
        import asyncio
        async def stop(data, ident, context):
            return {'continue': 'finish tests'}
        bridge = NativeHookBridge({'Stop': [HookMatcher(hooks=[stop])]})
        _, env = await bridge.start()
        path = os.path.join(os.path.dirname(__file__), '..', 'src', 'kiro_agent_sdk', '_hook_bridge.py')
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, path, env={**os.environ, **env},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await process.communicate(b'{\"hook_event_name\":\"Stop\"}')
            self.assertEqual(process.returncode, 0)
            self.assertEqual(json.loads(stdout), {'decision': 'block', 'reason': 'finish tests'})
        finally:
            await bridge.close()

    async def test_native_hook_context_and_ask(self):
        import asyncio
        async def context_hook(data, ident, context):
            return {'context': 'extra'}
        async def ask_hook(data, ident, context):
            return {'ask': 'review'}
        bridge = NativeHookBridge({'UserPromptSubmit': [HookMatcher(hooks=[context_hook])],
                                   'PreToolUse': [HookMatcher(hooks=[ask_hook])]}, supports_ask=True)
        _, env = await bridge.start()
        path = os.path.join(os.path.dirname(__file__), '..', 'src', 'kiro_agent_sdk', '_hook_bridge.py')
        async def invoke(event):
            process = await asyncio.create_subprocess_exec(sys.executable, path, env={**os.environ, **env},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await process.communicate(json.dumps({'hook_event_name': event}).encode())
            self.assertEqual(process.returncode, 0)
            return stdout.decode()
        try:
            self.assertEqual((await invoke('UserPromptSubmit')).strip(), 'extra')
            self.assertEqual(json.loads(await invoke('PreToolUse'))['hookSpecificOutput']['permissionDecision'], 'ask')
        finally:
            await bridge.close()

    async def test_native_hook_v3_is_registered(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', env={'HOME': root},
                                       native_hook_bridge_mode="same-user",
                                       native_hooks={"preToolUse": [HookMatcher()], 'postToolUse': [HookMatcher()],
                                                     'agentStop': [HookMatcher()], 'fileEdited': [HookMatcher()]})
            async with KiroSDKClient(options) as client:
                self.assertEqual(client._current_mode, client._agent_file.stem)
                hooks = json.loads(client._agent_file.read_text())['hooks']
                self.assertEqual({h['trigger'] for h in hooks}, {'PreToolUse', 'PostToolUse', 'Stop', 'PostFileSave'})
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3',
                                       env={'HOME': root, 'FAKE_NO_HOOK': '1'},
                                       native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher()]})
            client = KiroSDKClient(options)
            with self.assertRaisesRegex(ProtocolError, 'did not register'):
                await client.connect()
            self.assertFalse(client._connected)
            self.assertIsNone(client._agent_file)

    async def test_cli_listings(self):
        self.assertEqual((await list_sessions(cli_path=FAKE))[0]['sessionId'], 'one')
        self.assertEqual(await list_sessions(cli_path=FAKE, limit=0), [])
        with self.assertRaises(ValueError):
            await list_sessions('/tmp', cwd='/tmp', cli_path=FAKE)
        self.assertEqual((await get_session_info('one', cli_path=FAKE))['sessionId'], 'one')
        self.assertIsNone(await get_session_info('missing', cli_path=FAKE))
        self.assertEqual((await list_models(cli_path=FAKE))[0]['model_id'], 'example')
        await delete_session('one', cli_path=FAKE)
        with self.assertRaises(ValueError):
            await delete_session('--help', cli_path=FAKE)
        self.assertEqual((await get_session_history('one', cli_path=FAKE))[0]['content']['text'], 'replayed')
        messages = await get_session_messages('one', cli_path=FAKE)
        self.assertEqual(role(messages[0]), 'agent')
        self.assertIsNone(messages[0].message_id)
        self.assertEqual(messages[0].content[0].text, 'replayed')
        self.assertEqual(await get_session_messages('one', cli_path=FAKE, limit=0), [])
        with self.assertRaises(ValueError):
            await get_session_messages('one', cli_path=FAKE, offset=-1)

    async def test_temporary_agent_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            opts = KiroAgentOptions(cli_path=FAKE, env={'HOME': root}, agent_engine='v3',
                                    system_prompt='hello', tools=[])
            async with KiroSDKClient(opts) as client:
                path = client._agent_file
                self.assertTrue(path.is_file())
                self.assertEqual(json.loads(path.read_text())['prompt'], 'hello')
            self.assertFalse(path.exists())
            custom_home = os.path.join(root, 'custom-kiro')
            opts = KiroAgentOptions(cli_path=FAKE, env={'HOME': root, 'KIRO_HOME': custom_home},
                                    agent_engine='v3', system_prompt='hello')
            async with KiroSDKClient(opts) as client:
                path = client._agent_file
                self.assertEqual(path.parent, Path(custom_home) / 'agents')
            self.assertFalse(path.exists())

    async def test_modified_temporary_agent_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3',
                                       env={'HOME': root}, system_prompt='original')
            async with KiroSDKClient(options) as client:
                path = client._agent_file
                path.write_text('user edited this file', encoding='utf-8')
            self.assertEqual(path.read_text(encoding='utf-8'), 'user edited this file')

    async def test_agent_definitions_are_exclusive_and_cleaned(self):
        with tempfile.TemporaryDirectory() as root:
            opts = KiroAgentOptions(cli_path=FAKE, env={'HOME': root}, cwd=root,
                agents={'researcher': AgentDefinition(description='Research', prompt='Investigate', tools=['read'])})
            agent_path = os.path.join(root, '.kiro', 'agents', 'researcher.json')
            async with KiroSDKClient(opts):
                with open(agent_path, encoding='utf-8') as file:
                    self.assertEqual(json.load(file)['tools'], ['read'])
                with self.assertRaises(FileExistsError):
                    await KiroSDKClient(opts).connect()
            self.assertFalse(os.path.exists(agent_path))
            async with KiroSDKClient(opts):
                with open(agent_path, 'w', encoding='utf-8') as file:
                    file.write('user changed this')
            self.assertTrue(os.path.exists(agent_path))
            with self.assertRaises(ValueError):
                await KiroSDKClient(KiroAgentOptions(cli_path=FAKE, env={'HOME': root},
                    agents={'../escape': AgentDefinition('bad', 'bad')})).connect()

    async def test_agent_definition_tool_exclusions(self):
        with tempfile.TemporaryDirectory() as root:
            definition = AgentDefinition('Read only', 'Inspect', tools=['*'], disallowed_tools=['write'])
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', cwd=root,
                                       env={'HOME': root}, agents={'reviewer': definition})
            path = Path(root) / '.kiro' / 'agents' / 'reviewer.json'
            async with KiroSDKClient(options):
                self.assertEqual(json.loads(path.read_text())['excludedTools'], ['write'])

    async def test_agent_definition_kiro_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            rules = {'rules': [{'capability': 'shell', 'effect': 'deny'}]}
            definition = AgentDefinition('Restricted', 'Inspect', tools=['read'], kiro_permissions=rules)
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', cwd=root,
                                       env={'KIRO_HOME': str(Path(root) / 'kiro')},
                                       agents={'reviewer': definition})
            path = Path(root) / 'kiro' / 'agents' / 'reviewer.json'
            async with KiroSDKClient(options):
                self.assertEqual(json.loads(path.read_text())['permissions'], rules)
            self.assertFalse(path.exists())
            for engine, permissions in (
                ('v2', rules),
                ('v3', {'rules': [{'capability': 'unknown', 'effect': 'deny'}]}),
            ):
                invalid = AgentDefinition('Invalid', 'Inspect', kiro_permissions=permissions)
                client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine=engine,
                    cwd=root, env={'KIRO_HOME': str(Path(root) / 'kiro')}, agents={'bad': invalid}))
                with self.assertRaises(ValueError):
                    await client.connect()
            async def approve(*args):
                return None
            allow = AgentDefinition('Allow', 'Inspect',
                                    kiro_permissions={'rules': [{'capability': 'shell', 'effect': 'allow'}]})
            client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine='v3', cwd=root,
                env={'KIRO_HOME': str(Path(root) / 'kiro')}, agents={'allow': allow}, can_use_tool=approve))
            with self.assertRaisesRegex(ValueError, 'can bypass'):
                await client.connect()

    async def test_agent_definition_inline_mcp_servers(self):
        with tempfile.TemporaryDirectory() as root:
            servers = [{'inline': {'type': 'stdio', 'command': 'example-server', 'args': ['--stdio']}}]
            definition = AgentDefinition('MCP test', 'Inspect', mcp_servers=servers)
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', cwd=root,
                                       env={'KIRO_HOME': str(Path(root) / 'kiro')},
                                       agents={'reviewer': definition})
            path = Path(root) / 'kiro' / 'agents' / 'reviewer.json'
            async with KiroSDKClient(options):
                self.assertEqual(json.loads(path.read_text())['mcpServers'],
                                 {'inline': servers[0]['inline']})
            self.assertFalse(path.exists())
            invalid_values = [
                ['parent-server'],
                [{'x': {'type': 'sdk'}}],
                [{'x': {}}, {'x': {}}],
                [{'x': {'value': float('nan')}}],
                [{'x': {}, 'y': {}}],
            ]
            for servers in invalid_values:
                with self.subTest(servers=servers):
                    bad = AgentDefinition('Invalid', 'Inspect', mcp_servers=servers)
                    client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine='v3', cwd=root,
                        env={'KIRO_HOME': str(Path(root) / 'kiro')}, agents={'bad': bad}))
                    with self.assertRaises(ValueError):
                        await client.connect()
            client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine='v2', cwd=root,
                env={'KIRO_HOME': str(Path(root) / 'kiro')}, agents={'bad': definition}))
            with self.assertRaises(ValueError):
                await client.connect()

    async def test_kiro_resources(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', env={'HOME': root},
                                       kiro_resources=['file:///tmp/context.md', 'skill://example'])
            async with KiroSDKClient(options) as client:
                self.assertEqual(json.loads(client._agent_file.read_text())['resources'], options.kiro_resources)
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(kiro_resources=['https://example.com']))

    async def test_resumed_agent_is_reapplied(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cli_path=FAKE, env={'HOME': root}, resume='old',
                                       system_prompt='test', tools=[])
            async with KiroSDKClient(options) as client:
                modes = [event for event in client.history if event.get('sessionUpdate') == 'current_mode_update']
                self.assertEqual(modes[0]['currentModeId'], client._agent_file.stem)

    async def test_additional_directories(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cli_path=FAKE, cwd=root, agent_engine='v3', add_dirs=['shared'])
            async with KiroSDKClient(options) as client:
                expected = [os.path.join(root, 'shared')]
                self.assertEqual(client.session_info['received']['additionalDirectories'], expected)
                self.assertEqual((await client.fork_session())['received']['additionalDirectories'], expected)
                fork = await client.fork_session(message_id='message-1', title='branch')
                self.assertEqual(fork['received']['_meta']['kiro'], {'messageId': 'message-1', 'title': 'branch'})
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(add_dirs=['shared']))

    async def test_v3_session_controls(self):
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine='v3')) as client:
            self.assertTrue((await client.rename_session('title'))['success'])
            self.assertTrue((await client.compact())['success'])
            self.assertEqual((await client.get_history_page(limit=2))['updates'], [])
            self.assertEqual((await client.export_session())['filePath'], '/tmp/export.zip')
            with self.assertRaises(ValueError):
                await client.get_history_page(limit=0)
            self.assertTrue((await client.reconnect_mcp_server('test'))['success'])
            self.assertEqual((await client.get_mcp_resource('test', 'memory://a'))['contents'][0]['text'], 'resource')
            self.assertEqual((await client.get_mcp_prompt('test', 'hello'))['messages'][0]['content']['text'], 'hello')
            with self.assertRaises(ProtocolError):
                await client.rewind_files('msg')
            self.assertEqual((await client.rewind_files('msg', restore_conversation=True))['totalFiles'], 1)
            with self.assertRaises(ValueError):
                await client.rename_session(' ')
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE)) as client:
            with self.assertRaises(ProtocolError):
                await client.compact()

    async def test_checkpoint_capability_option(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', enable_file_checkpointing=True)
        async with KiroSDKClient(options) as client:
            self.assertTrue((await client.restore_checkpoint('msg'))['success'])
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(enable_file_checkpointing=True))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', enable_file_checkpointing=False))

    async def test_mcp_http_headers_and_capability_checks(self):
        server = {'type': 'http', 'url': 'http://127.0.0.1:1234', 'headers': {'X-Test': 'value'}}
        async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, mcp_servers={'http': server})) as client:
            sent = client.session_info['received']['mcpServers'][0]
            self.assertEqual(sent['headers'], [{'name': 'X-Test', 'value': 'value'}])
        with self.assertRaisesRegex(ProtocolError, 'MCP sse'):
            await KiroSDKClient(KiroAgentOptions(cli_path=FAKE,
                mcp_servers={'sse': {'type': 'sse', 'url': 'http://localhost'}})).connect()

    async def test_required_sandbox_fails_closed(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', require_sandbox=True)
        async with KiroSDKClient(options) as client:
            self.assertEqual((await client.get_sandbox_status())['status'], 'active')
            with self.assertRaises(ProtocolError):
                await client.set_config_option('sandbox', 'disabled')
            client._sandbox_status = {'status': 'unavailable'}
            with self.assertRaisesRegex(ProtocolError, 'not active'):
                await client.query('hello')
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', require_sandbox=True,
                                   env={'FAKE_SANDBOX_UNAVAILABLE': '1'})
        client = KiroSDKClient(options)
        with self.assertRaisesRegex(ProtocolError, 'sandbox unavailable'):
            await client.connect()
        self.assertFalse(client._connected)
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(require_sandbox=True))

    async def test_plan_mode(self):
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(permission_mode='plan'))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', permission_mode='plan',
                                           native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher()]}))
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', permission_mode='plan')
        async with KiroSDKClient(options) as client:
            self.assertEqual(client._current_mode, 'plan')
            await client.set_mode('vibe')
            await client.set_permission_mode('ask')
            await client.set_permission_mode('plan')
            await client.set_permission_mode('ask')
            self.assertEqual(client._current_mode, 'vibe')

    async def test_thinking_option(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', thinking=False, effort='low')
        async with KiroSDKClient(options) as client:
            self.assertFalse(client.session_info['received']['_meta']['kiro']['thinkingEnabled'])
            self.assertTrue(any(option['id'] == 'effortLevel' and option['currentValue'] == 'low'
                                for option in client.session_info['configOptions']))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', thinking={'type': 'enabled'}))

    async def test_kiro_permissions_profile(self):
        with tempfile.TemporaryDirectory() as root:
            policy = {'rules': [{'capability': 'fs_write', 'effect': 'deny'}]}
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3', env={'HOME': root},
                                       kiro_permissions=policy)
            async with KiroSDKClient(options) as client:
                self.assertEqual(json.loads(client._agent_file.read_text())['permissions'], policy)
                self.assertEqual((await client.list_kiro_permissions())['rules'][0]['effect'], 'deny')
                self.assertEqual((await client.explain_kiro_permission('fs_write', 'a'))['effect'], 'deny')
                with self.assertRaises(ProtocolError):
                    await client.set_mode('vibe')
                with self.assertRaises(ProtocolError):
                    await client.set_config_option('mode', 'vibe')
            options = KiroAgentOptions(cli_path=FAKE, agent_engine='v3',
                                       env={'HOME': root, 'FAKE_NO_POLICY': '1'}, kiro_permissions=policy)
            client = KiroSDKClient(options)
            with self.assertRaisesRegex(ProtocolError, 'did not register'):
                await client.connect()
            self.assertFalse(client._connected)
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(kiro_permissions={'rules': []}))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', kiro_permissions={
                'rules': [{'capability': 'typo', 'effect': 'deny'}]}))

    async def test_structured_output(self):
        options = KiroAgentOptions(cli_path=FAKE, output_format={
            'type': 'json_schema', 'schema': {'type': 'object', 'required': ['x'],
                                             'properties': {'x': {'type': 'integer'}}}})
        async with KiroSDKClient(options) as client:
            await client.query('{"x": 3}')
            messages = [message async for message in client.receive_response()]
            self.assertEqual(messages[-1].structured_output, {'x': 3})
            await client.query('{"x": "wrong"}')
            with self.assertRaises(ResultError):
                _ = [message async for message in client.receive_response()]

    async def test_structured_output_rejects_invalid_json_constants_and_duplicate_keys(self):
        for text in ('NaN', '{"a": 1, "a": 2}'):
            options = KiroAgentOptions(cli_path=FAKE, output_format={'type': 'json_schema', 'schema': {}})
            async with KiroSDKClient(options) as client:
                await client.query(text)
                with self.assertRaises(ResultError):
                    _ = [message async for message in client.receive_response()]

    async def test_prompt_block_and_stop(self):
        events = []
        async def hook(data, tool_id, context):
            events.append(data['hook_event_name'])
            if data.get('prompt') == 'blocked':
                return {'block': True}
            return {}
        matcher = HookMatcher(hooks=[hook])
        options = KiroAgentOptions(cli_path=FAKE, hooks={'promptSubmit':[matcher], 'agentStop':[matcher]})
        async with KiroSDKClient(options) as client:
            with self.assertRaises(ProtocolError):
                await client.query('blocked')
            await client.query('ok')
            _ = [message async for message in client.receive_response()]
        self.assertEqual(events, ['UserPromptSubmit','UserPromptSubmit','Stop'])

    async def test_permission_hook_blocks(self):
        async def deny(data, tool_id, context):
            return {'block': True}
        options = KiroAgentOptions(cli_path=FAKE, permission_mode='trustAll',
                                   hooks={'preToolUse':[HookMatcher(hooks=[deny])]})
        async with KiroSDKClient(options) as client:
            await client.query('permission')
            messages = [message async for message in client.receive_response()]
            self.assertEqual(messages[-1].result, 'no')

class NativeMcpInstanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_instance_unknown_request_does_not_log_private_data(self):
        try:
            import mcp.types as types
            from mcp.server import Server
        except ImportError:
            self.skipTest('optional mcp dependency not installed')
        if hasattr(types, 'jsonrpc_message_adapter'):
            self.skipTest('legacy MCP request handling')
        bridge = SdkMcpBridge({'type': 'sdk', 'name': 'example', 'instance': Server('example')})
        config = await bridge.start()
        env = {entry['name']: entry['value'] for entry in config['env']}
        reader, writer = await asyncio.open_connection('127.0.0.1', int(env['KIRO_SDK_MCP_PORT']))
        writer.write((env['KIRO_SDK_MCP_TOKEN'] + '\n').encode())

        async def send(message):
            writer.write((json.dumps(message) + '\n').encode())
            await writer.drain()

        async def receive():
            line = await asyncio.wait_for(reader.readline(), 5)
            self.assertTrue(line)
            return json.loads(line)

        try:
            await send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                        'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                                   'clientInfo': {'name': 'test', 'version': '1'}}})
            self.assertIn('result', await receive())
            await send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            with self.assertNoLogs(level='WARNING'):
                await send({'jsonrpc': '2.0', 'id': 2, 'method': 'server/discover',
                            'params': {'private': 'TEST_PRIVATE_VALUE'}})
                response = await receive()
            self.assertEqual(response['error']['code'], -32601)
            self.assertNotIn('TEST_PRIVATE_VALUE', str(response))
            with self.assertNoLogs(level='WARNING'):
                await send({'jsonrpc': '2.0', 'method': 'notifications/unknown',
                            'params': {'private': 'TEST_PRIVATE_VALUE'}})
                await send({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
                self.assertEqual((await receive())['result'], {})
            with self.assertNoLogs(level='WARNING'):
                writer.write(b'{"jsonrpc":"2.0","id":4,"method":"ping",'
                             b'"method":"server/discover","params":{"private":"TEST_PRIVATE_VALUE"}}\n')
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readline(), 5), b'')
        finally:
            writer.close()
            await writer.wait_closed()
            await bridge.close()

    async def test_mcp_tool_annotations_object(self):
        try:
            from mcp.types import ToolAnnotations
        except ImportError:
            self.skipTest('optional mcp dependency not installed')
        @tool('inspect', 'Inspect', {}, annotations=ToolAnnotations(readOnlyHint=True))
        async def inspect(args):
            return {'content': []}
        bridge = SdkMcpBridge(create_mcp_server('test', tools=[inspect]))
        result = await bridge._dispatch({'id': 1, 'method': 'tools/list'})
        self.assertTrue(result['result']['tools'][0]['annotations']['readOnlyHint'])

    async def test_mcp_instance_resources(self):
        import asyncio
        try:
            import mcp.types as types
            from mcp.server import Server
        except ImportError:
            self.skipTest('optional mcp dependency not installed')
        resource = types.Resource(uri='memory://example', name='example')
        if hasattr(types, 'jsonrpc_message_adapter'):
            async def list_resources(ctx, params):
                return types.ListResourcesResult(resources=[resource])
            server = Server('example', on_list_resources=list_resources)
        else:
            server = Server('example')
            @server.list_resources()
            async def list_resources():
                return [resource]
        bridge = SdkMcpBridge({'type': 'sdk', 'name': 'example', 'instance': server})
        config = await bridge.start()
        env = {entry['name']: entry['value'] for entry in config['env']}
        process = await asyncio.create_subprocess_exec(config['command'], *config['args'],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env={**os.environ, **env})
        async def request(ident, method, params):
            process.stdin.write((json.dumps({'jsonrpc':'2.0','id':ident,'method':method,'params':params})+'\n').encode())
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), 5)
            self.assertTrue(line, 'bridge exited before responding')
            return json.loads(line)
        try:
            init = await request(1, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                                    'clientInfo': {'name':'test', 'version':'1'}})
            self.assertIn('result', init)
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await process.stdin.drain()
            result = await request(2, 'resources/list', {})
            self.assertEqual(result['result']['resources'][0]['name'], 'example')
        finally:
            process.stdin.close()
            await process.wait()
            await bridge.close()
