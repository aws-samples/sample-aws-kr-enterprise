import asyncio
import json
import unittest
from kiro_agent_sdk import AgentMessage, CLIConnectionError, HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError, ResultMessage, StreamEvent, TextBlock, Transport, query

class FakeTransport(Transport):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.ready = False
        self.closed = False
        self.requests = []
    async def connect(self): self.ready = True
    async def write(self, data):
        message = json.loads(data)
        self.requests.append(message)
        method = message['method']
        if method == 'initialize':
            result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}}
        elif method == 'session/new':
            mode = message['params'].get('_meta', {}).get('kiro', {}).get('modeId', 'vibe')
            model = message['params'].get('_meta', {}).get('kiro', {}).get('modelId', 'auto')
            result = {'sessionId': 'remote', 'modes': {'currentModeId': mode},
                      'configOptions': [{'id': 'model', 'currentValue': model}]}
        elif method == 'session/prompt':
            await self.queue.put({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                'sessionId': 'remote', 'update': {'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': message['params']['prompt'][0]['text']}}}})
            result = {'stopReason': 'end_turn'}
        elif method == 'session/set_config_option':
            result = {'configOptions': [{'id': message['params']['configId'],
                                         'currentValue': message['params']['value']}]}
        else:
            result = {}
        await self.queue.put({'jsonrpc':'2.0', 'id': message['id'], 'result': result})
    async def read_messages(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item
    async def close(self):
        self.closed = True
        self.ready = False
        await self.queue.put(None)
    def is_ready(self): return self.ready
    async def end_input(self): pass

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_transport_preserves_remote_paths(self):
        class RemotePaths(FakeTransport):
            async def write(self, data):
                message = json.loads(data)
                if message.get("method") == "initialize":
                    self.requests.append(message)
                    await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "protocolVersion": 1, "agentCapabilities": {"loadSession": True,
                        "sessionCapabilities": {"list": {}, "fork": {}}}}})
                else:
                    await super().write(data)

        observed = []
        async def hook(event, tool_id, context):
            observed.append(event["cwd"])
            return {}
        remote_cwd = r"C:\workspace\project"
        remote_dirs = [r"C:\shared\data", "/srv/shared"]
        transport = RemotePaths()
        options = KiroAgentOptions(agent_engine="v3", cwd=remote_cwd, add_dirs=remote_dirs,
                                   hooks={"promptSubmit": [HookMatcher(hooks=[hook])]})
        async with KiroSDKClient(options, transport=transport) as client:
            request = next(item for item in transport.requests if item["method"] == "session/new")
            self.assertEqual(request["params"]["cwd"], remote_cwd)
            self.assertEqual(request["params"]["additionalDirectories"], remote_dirs)
            await client.query("hello")
            _ = [message async for message in client.receive_response()]
            await client.list_sessions(cwd=remote_cwd)
            await client.fork_session()
            listing = next(item for item in transport.requests if item["method"] == "session/list")
            fork = next(item for item in transport.requests if item["method"] == "session/fork")
            self.assertEqual(listing["params"]["cwd"], remote_cwd)
            self.assertEqual(fork["params"]["cwd"], remote_cwd)
            self.assertEqual(fork["params"]["additionalDirectories"], remote_dirs)
        self.assertEqual(observed, [remote_cwd])

    async def test_custom_transport_rejects_relative_remote_paths(self):
        for values in ({"cwd": "relative"}, {"cwd": "/remote", "add_dirs": ["relative"]}):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "absolute paths"):
                    KiroSDKClient(KiroAgentOptions(agent_engine="v3", **values), transport=FakeTransport())

    async def test_subagent_text_without_parent_mapping_is_not_root_text(self):
        class ChildFirst(FakeTransport):
            async def write(self, data):
                message = json.loads(data)
                if message['method'] == 'session/prompt':
                    await self.queue.put({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                        'sessionId': 'remote', 'update': {'sessionUpdate': 'agent_message_chunk',
                        'content': {'type': 'text', 'text': 'child text'},
                        '_meta': {'kiro': {'agentSubtaskId': 'child-1'}}}}})
                await super().write(data)
        options = KiroAgentOptions(agent_engine='v3', forward_subagent_text=True, include_partial_messages=True)
        async with KiroSDKClient(options, transport=ChildFirst()) as client:
            await client.query('root')
            messages = [message async for message in client.receive_response()]
            child = next(message for message in messages if isinstance(message, AgentMessage) and
                         message.agent_id == 'child-1')
            self.assertIsNone(child.parent_tool_call_id)
            self.assertEqual(child.content[0], TextBlock('child text'))
            self.assertTrue(any(isinstance(message, StreamEvent) and message.agent_id == 'child-1'
                                for message in messages))
            self.assertEqual(messages[-1].result, 'root')

    async def test_v3_context_usage_preserves_raw_and_adds_percentage(self):
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine='v3'), transport=transport) as client:
            self.assertIsNone(await client.get_context_usage())
            update = {'sessionUpdate': 'session_info_update', '_meta': {'kiro': {
                'kind': 'context_usage', 'contextUsage': {'usagePercentage': 12.5},
                'breakdown': {'tools': {'tokens': 123, 'percent': 1.25, 'mcp': {'tokens': 50}},
                              'memory': {'tokens': True}, 'contextFiles': {'tokens': -1}}}}}
            await transport.queue.put({'jsonrpc': '2.0', 'method': 'session/update',
                                       'params': {'sessionId': 'remote', 'update': update}})
            await asyncio.wait_for(client._events.get(), 2)
            usage = await client.get_context_usage()
            self.assertEqual(usage['percentage'], 12.5)
            self.assertEqual(usage['_meta']['kiro']['breakdown']['tools']['tokens'], 123)
            self.assertEqual(usage['categories'], [{'name': 'tools', 'tokens': 123, 'kiroPercent': 1.25}])
            self.assertNotIn('totalTokens', usage)
            self.assertNotIn('percentage', await client.get_context_usage_raw())
            self.assertNotIn('categories', await client.get_context_usage_raw())
            usage['_meta']['kiro']['breakdown']['tools']['tokens'] = 0
            self.assertEqual((await client.get_context_usage_raw())['_meta']['kiro']['breakdown']['tools']['tokens'], 123)
            update['_meta']['kiro']['contextUsage']['usagePercentage'] = None
            update['_meta']['kiro']['usagePercentage'] = 17
            await transport.queue.put({'jsonrpc': '2.0', 'method': 'session/update',
                                       'params': {'sessionId': 'remote', 'update': update}})
            await asyncio.wait_for(client._events.get(), 2)
            self.assertEqual((await client.get_context_usage())['percentage'], 17.0)

    async def test_v2_context_metadata_is_session_scoped(self):
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine='v2'), transport=transport) as client:
            async def deliver(session_id, percent):
                await transport.queue.put({'jsonrpc': '2.0', 'method': '_kiro.dev/metadata',
                                           'params': {'sessionId': session_id, 'contextUsagePercentage': percent}})
                await asyncio.wait_for(client._events.get(), 2)
            await deliver('other', 90)
            self.assertIsNone(await client.get_context_usage())
            await deliver('remote', 7.25)
            await deliver('other', 99)
            self.assertEqual((await client.get_context_usage())['percentage'], 7.25)
            await deliver('remote', float('nan'))
            self.assertNotIn('percentage', await client.get_context_usage())

    async def test_mcp_status_snapshots_are_normalized_and_session_scoped(self):
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine='v3'), transport=transport) as client:
            self.assertIsNone(await client.get_mcp_status())
            async def deliver(session_id, servers):
                await transport.queue.put({'jsonrpc': '2.0', 'method': '_kiro/mcp/status',
                                           'params': {'sessionId': session_id, 'servers': servers}})
                await asyncio.wait_for(client._events.get(), 2)
            await deliver('other-session', [{'name': 'unrelated', 'status': 'connected'}])
            self.assertIsNone(await client.get_mcp_status())
            await deliver('remote', [
                {'name': 'ready', 'status': 'connected', 'tools': [{'name': 'ping'}, {'description': 'invalid'}],
                 'serverInfo': {'name': 'incomplete'}},
                {'name': 'loading', 'status': 'connecting'},
                {'name': 'auth', 'status': 'failed', 'failedAuthorization': True, 'errorMessage': 'Sign in'},
                {'name': 'future', 'status': 'new-status'},
            ])
            status = await client.get_mcp_status()
            self.assertEqual([item['status'] for item in status['mcpServers']],
                             ['connected', 'pending', 'needs-auth', 'unknown'])
            self.assertEqual(status['mcpServers'][2]['error'], 'Sign in')
            self.assertEqual(status['mcpServers'][1]['kiroStatus'], 'connecting')
            self.assertEqual(status['mcpServers'][0]['tools'], [{'name': 'ping'}])
            self.assertNotIn('serverInfo', status['mcpServers'][0])
            self.assertEqual((await client.get_mcp_status_raw())['servers'][1]['status'], 'connecting')
            self.assertEqual(len((await client.get_mcp_status_raw())['servers'][0]['tools']), 2)
            status['mcpServers'][0]['name'] = 'changed'
            self.assertEqual((await client.get_mcp_status())['mcpServers'][0]['name'], 'ready')
            await deliver('other-session', [{'name': 'unrelated', 'status': 'connected'}])
            self.assertEqual((await client.get_mcp_status())['mcpServers'][0]['name'], 'ready')
            await deliver('remote', [{'name': 'only', 'status': 'disabled'}])
            self.assertEqual([item['name'] for item in (await client.get_mcp_status())['mcpServers']], ['only'])
            await transport.queue.put({'jsonrpc': '2.0', 'method': '_kiro.dev/mcp/server_initialized',
                                       'params': {'sessionId': 'remote', 'serverName': 'only'}})
            await asyncio.wait_for(client._events.get(), 2)
            self.assertEqual((await client.get_mcp_status())['mcpServers'][0]['status'], 'connected')

    async def test_v2_mcp_status_aggregates_per_server_notifications(self):
        transport = FakeTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine='v2'), transport=transport) as client:
            async def deliver(method, server, session='remote', **fields):
                await transport.queue.put({'jsonrpc': '2.0', 'method': method,
                    'params': {'sessionId': session, 'serverName': server, **fields}})
                await asyncio.wait_for(client._events.get(), 2)
            await deliver('_kiro.dev/mcp/server_initialized', 'a')
            await deliver('_kiro.dev/mcp/server_initialized', 'b')
            await deliver('_kiro.dev/mcp/server_init_failure', 'a', error='failed')
            await deliver('_kiro.dev/mcp/oauth_request', 'b')
            await deliver('_kiro.dev/mcp/server_initialized', 'other', session='other-session')
            status = await client.get_mcp_status()
            self.assertEqual({item['name']: item['status'] for item in status['mcpServers']},
                             {'a': 'failed', 'b': 'needs-auth'})
            self.assertEqual(status['mcpServers'][0]['error'], 'failed')

    async def test_mcp_snapshot_before_session_response_is_retained(self):
        class EarlyStatus(FakeTransport):
            async def write(self, data):
                message = json.loads(data)
                if message['method'] == 'session/new':
                    await self.queue.put({'jsonrpc': '2.0', 'method': '_kiro/mcp/status',
                        'params': {'sessionId': 'remote', 'servers': [{'name': 'early', 'status': 'connected'}]}})
                await super().write(data)
        async with KiroSDKClient(KiroAgentOptions(agent_engine='v3'), transport=EarlyStatus()) as client:
            self.assertEqual((await client.get_mcp_status())['mcpServers'][0]['name'], 'early')

    async def test_custom_transport(self):
        transport = FakeTransport()
        messages = [message async for message in query(prompt='remote works', transport=transport)]
        self.assertIsInstance(messages[-1], ResultMessage)
        self.assertEqual(messages[-1].result, 'remote works')
        self.assertTrue(transport.closed)

    async def test_local_only_settings_rejected(self):
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(system_prompt='not supported'), transport=FakeTransport())
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(mcp_servers={'x': {'type': 'sdk'}}), transport=FakeTransport())
        with self.assertRaisesRegex(ValueError, 'locally verified'):
            KiroSDKClient(KiroAgentOptions(agent_engine='v3', kiro_shell_edit=True), transport=FakeTransport())

    async def test_v3_session_configuration(self):
        transport = FakeTransport()
        opts = KiroAgentOptions(agent_engine='v3', agent='reviewer', model='example', effort='high')
        async with KiroSDKClient(opts, transport=transport):
            request = next(r for r in transport.requests if r['method'] == 'session/new')
            self.assertEqual(request['params']['_meta']['kiro'],
                             {'modeId': 'reviewer', 'modelId': 'example', 'effortLevel': 'high'})
            init = next(r for r in transport.requests if r['method'] == 'initialize')
            self.assertTrue(init['params']['clientCapabilities']['_meta']['kiro']['hooks']['enabled'])

    async def test_close_failure_still_cleans_up(self):
        class BadClose(FakeTransport):
            async def close(self):
                await super().close()
                raise RuntimeError('close failed')
        client = KiroSDKClient(transport=BadClose())
        await client.connect()
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            await client.disconnect()
        self.assertFalse(client._connected)
        self.assertIsNone(client.session_id)

    async def test_remote_turn_timeout_cancels_and_closes_transport(self):
        class SlowTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.cancel_seen = False
                self.closed_event = asyncio.Event()
            async def write(self, data):
                message = json.loads(data)
                if message['method'] == 'session/prompt':
                    await asyncio.Event().wait()
                elif message['method'] == 'session/cancel':
                    self.cancel_seen = True
                else:
                    await super().write(data)
            async def close(self):
                await super().close()
                self.closed_event.set()

        transport = SlowTransport()
        client = KiroSDKClient(KiroAgentOptions(turn_timeout=0.1), transport=transport)
        await client.connect()
        try:
            await client.query('wait')
            await asyncio.wait_for(transport.closed_event.wait(), 2)
            self.assertTrue(transport.cancel_seen)
            with self.assertRaisesRegex(CLIConnectionError, 'Timed out waiting for the Kiro turn'):
                _ = [message async for message in client.receive_response()]
        finally:
            await client.disconnect()

    async def test_turn_timeout_when_remote_write_ignores_cancellation(self):
        class SlowTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.release = asyncio.Event()
                self.closed_event = asyncio.Event()
            async def write(self, data):
                message = json.loads(data)
                if message['method'] == 'session/prompt':
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        await self.release.wait()
                elif message['method'] != 'session/cancel':
                    await super().write(data)
            async def close(self):
                await super().close()
                self.closed_event.set()

        transport = SlowTransport()
        client = KiroSDKClient(KiroAgentOptions(turn_timeout=0.1), transport=transport)
        await client.connect()
        try:
            await asyncio.wait_for(client.query('wait'), 2)
            await asyncio.wait_for(transport.closed_event.wait(), 2)
            with self.assertRaisesRegex(CLIConnectionError, 'Timed out waiting for the Kiro turn'):
                _ = [message async for message in client.receive_response()]
        finally:
            transport.release.set()
            await client.disconnect()

    async def test_context_change_finishes_before_a_new_prompt(self):
        class SlowContext(FakeTransport):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()
            async def write(self, data):
                if json.loads(data)['method'] == '_kiro/session/context':
                    self.started.set()
                    await self.release.wait()
                await super().write(data)

        transport = SlowContext()
        async with KiroSDKClient(KiroAgentOptions(agent_engine='v3'), transport=transport) as client:
            update = asyncio.create_task(client.add_kiro_context('file.txt'))
            await asyncio.wait_for(transport.started.wait(), 2)
            prompt = asyncio.create_task(client.query('hello'))
            await asyncio.sleep(0.05)
            self.assertFalse(any(request['method'] == 'session/prompt' for request in transport.requests))
            transport.release.set()
            await update
            await prompt
            self.assertEqual([message async for message in client.receive_response()][-1].result, 'hello')

    async def test_checkpoint_restore_finishes_before_a_new_prompt(self):
        class SlowCheckpoint(FakeTransport):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()
            async def write(self, data):
                message = json.loads(data)
                if message['method'] == 'initialize':
                    result = {'protocolVersion': 1, 'agentCapabilities': {
                        'loadSession': True, '_meta': {'kiro': {'checkpoints': True}}}}
                    await self.queue.put({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
                elif message['method'] == '_kiro/checkpoint/revertMultiple':
                    self.started.set()
                    await self.release.wait()
                    await self.queue.put({'jsonrpc': '2.0', 'id': message['id'], 'result': {'success': True}})
                else:
                    await super().write(data)

        transport = SlowCheckpoint()
        async with KiroSDKClient(KiroAgentOptions(agent_engine='v3'), transport=transport) as client:
            restore = asyncio.create_task(client.restore_checkpoint('message-1'))
            await asyncio.wait_for(transport.started.wait(), 2)
            prompt = asyncio.create_task(client.query('hello'))
            await asyncio.sleep(0.05)
            self.assertFalse(any(request['method'] == 'session/prompt' for request in transport.requests))
            transport.release.set()
            self.assertTrue((await restore)['success'])
            await prompt
            self.assertEqual([message async for message in client.receive_response()][-1].result, 'hello')

    async def test_remote_sandbox_loss_cancels_and_closes_transport(self):
        class GuardTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.cancel_seen = False
                self.closed_event = asyncio.Event()
            async def write(self, data):
                message = json.loads(data)
                if message['method'] == '_kiro/sandbox/applyConfig':
                    await self.queue.put({'jsonrpc': '2.0', 'method': '_kiro/sandbox/status',
                                          'params': {'sessionId': 'remote', 'status': 'active'}})
                    await self.queue.put({'jsonrpc': '2.0', 'id': message['id'], 'result': {}})
                elif message['method'] == 'session/prompt':
                    return
                elif message['method'] == 'session/cancel':
                    self.cancel_seen = True
                else:
                    await super().write(data)
            async def close(self):
                await super().close()
                self.closed_event.set()

        transport = GuardTransport()
        options = KiroAgentOptions(agent_engine='v3', require_sandbox=True)
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query('wait')
            await transport.queue.put({'jsonrpc': '2.0', 'method': '_kiro/sandbox/status',
                                       'params': {'sessionId': 'remote', 'status': 'unavailable'}})
            await asyncio.wait_for(transport.closed_event.wait(), 3)
            self.assertTrue(transport.cancel_seen)
            with self.assertRaisesRegex(ProtocolError, 'sandbox stopped'):
                _ = [message async for message in client.receive_response()]
