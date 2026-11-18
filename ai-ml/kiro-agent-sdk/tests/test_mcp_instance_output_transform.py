import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient

FAKE = str(Path(__file__).with_name('fake_kiro.py'))


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP dependency')
class McpInstanceOutputTransformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import mcp.types as types
        from mcp.server import Server
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.started, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.calls = []
        self.result_types = {}
        tool_info = types.Tool(name='probe', description='Probe', inputSchema={
            'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']})
        async def tool_result(value):
            if value == 'needs' and hasattr(types, 'InputRequiredResult'):
                return types.InputRequiredResult(requestState='opaque')
            return types.CallToolResult(content=[types.TextContent(type='text', text='ORIGINAL_' + value)])
        if hasattr(types, 'jsonrpc_message_adapter'):
            async def list_tools(ctx, params):
                return types.ListToolsResult(tools=[tool_info])
            async def call_tool(ctx, params):
                return await tool_result(params.arguments['value'])
            self.server = Server('probe', on_list_tools=list_tools, on_call_tool=call_tool)
        else:
            self.server = Server('probe')
            @self.server.list_tools()
            async def list_tools():
                return [tool_info]
            @self.server.call_tool()
            async def call_tool(name, arguments):
                return await tool_result(arguments['value'])
        async def transform(server, name, arguments, result):
            value = arguments['value']
            self.calls.append((server, name, value, result['content'][0]['text']))
            self.result_types[value] = result.get('resultType')
            if value == 'slow':
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()
                self.finished.set()
            if value == 'fail':
                raise RuntimeError('SECRET_EXCEPTION')
            if value == 'badtype':
                return {'content': [{'type': 'text', 'text': 'invalid'}], 'resultType': 'input_required'}
            return {'content': [{'type': 'text', 'text': 'REPLACED_' + value}]}
        options = KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine='v3',
            env={'KIRO_HOME': str(self.root / 'kiro')},
            mcp_servers={'probe': {'type': 'sdk', 'name': 'probe', 'instance': self.server}},
            sdk_mcp_output_transforms={'probe': transform})
        self.client = KiroSDKClient(options)
        await self.client.connect()
        bridge = self.client._named_mcp_bridges['probe'][0]
        port = bridge.server.sockets[0].getsockname()[1]
        self.reader, self.writer = await asyncio.open_connection('127.0.0.1', port)
        self.writer.write((bridge.token + '\n').encode())
        version = '2026-07-28' if hasattr(types, 'InputRequiredResult') else '2025-03-26'
        await self.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': version, 'capabilities': {},
                       'clientInfo': {'name': 'test', 'version': '1'}}})
        initialized = await self.read()
        self.assertIn('result', initialized)
        await self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    async def asyncTearDown(self):
        self.release.set()
        self.writer.close()
        await self.writer.wait_closed()
        await self.client.disconnect()
        self.tmp.cleanup()

    async def send(self, data):
        self.writer.write((json.dumps(data) + '\n').encode())
        await self.writer.drain()

    async def read(self):
        line = await asyncio.wait_for(self.reader.readline(), 5)
        self.assertTrue(line, 'bridge closed unexpectedly')
        return json.loads(line)

    async def call(self, ident, value):
        await self.send({'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                         'params': {'name': 'probe', 'arguments': {'value': value}}})

    async def test_instance_result_is_replaced_and_errors_do_not_expose_original(self):
        await self.call(2, 'one')
        result = await self.read()
        self.assertEqual((result['id'], result['result']['content'][0]['text']), (2, 'REPLACED_one'))
        if self.result_types['one'] is not None:
            self.assertEqual(result['result']['resultType'], 'complete')
        self.assertEqual(self.calls, [('probe', 'probe', 'one', 'ORIGINAL_one')])
        await self.call(3, 'fail')
        result = await self.read()
        self.assertTrue(result['result']['isError'])
        self.assertNotIn('ORIGINAL_fail', str(result))
        self.assertNotIn('SECRET_EXCEPTION', str(result))
        if self.result_types['one'] is not None:
            await self.call(4, 'badtype')
            result = await self.read()
            self.assertTrue(result['result']['isError'])
            self.assertEqual(result['result']['resultType'], 'complete')

    async def test_cancellation_suppresses_late_transform_and_other_requests_continue(self):
        await self.call(2, 'slow')
        await asyncio.wait_for(self.started.wait(), 5)
        await self.call(3, 'fast')
        fast = await self.read()
        self.assertEqual((fast['id'], fast['result']['content'][0]['text']), (3, 'REPLACED_fast'))
        await self.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 2}})
        cancelled = await self.read()
        self.assertEqual((cancelled['id'], cancelled['error']['code']), (2, -32800))
        self.release.set()
        await asyncio.wait_for(self.finished.wait(), 2)
        await self.send({'jsonrpc': '2.0', 'id': 4, 'method': 'ping'})
        result = await self.read()
        self.assertEqual((result['id'], result['result']), (4, {}))

    async def test_sdk_cancellation_suppresses_late_transform(self):
        await self.call(2, 'slow')
        await asyncio.wait_for(self.started.wait(), 5)
        await self.client._named_mcp_bridges['probe'][0].request_tool_cancellation()
        cancelled = await self.read()
        self.assertEqual((cancelled['id'], cancelled['error']['code']), (2, -32800))
        self.release.set()
        await asyncio.wait_for(self.finished.wait(), 2)
        await self.send({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
        result = await self.read()
        self.assertEqual((result['id'], result['result']), (3, {}))

    async def test_input_required_result_is_not_transformed(self):
        import mcp.types as types
        if not hasattr(types, 'InputRequiredResult'):
            self.skipTest('MCP implementation does not support input-required results')
        bridge = self.client._named_mcp_bridges['probe'][0]
        reader, writer = await asyncio.open_connection('127.0.0.1', bridge.server.sockets[0].getsockname()[1])
        try:
            request = {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {
                'name': 'probe', 'arguments': {'value': 'needs'}, '_meta': {
                    'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                    'io.modelcontextprotocol/clientCapabilities': {}}}}
            writer.write((bridge.token + '\n' + json.dumps(request) + '\n').encode())
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(), 5))
            self.assertEqual(result['result']['resultType'], 'input_required')
            self.assertEqual(self.calls, [])
            request['id'] = 8
            request['params']['arguments']['value'] = 'one'
            writer.write((json.dumps(request) + '\n').encode())
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(), 5))
            self.assertEqual((result['result']['resultType'], result['result']['content'][0]['text']),
                             ('complete', 'REPLACED_one'))
            request['id'] = 9
            request['params']['arguments']['value'] = 'badtype'
            writer.write((json.dumps(request) + '\n').encode())
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(), 5))
            self.assertEqual((result['result']['resultType'], result['result']['isError']), ('complete', True))
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_duplicate_request_id_closes_before_leaking_a_result(self):
        await self.call(2, 'slow')
        await asyncio.wait_for(self.started.wait(), 5)
        await self.send({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
        line = await asyncio.wait_for(self.reader.readline(), 5)
        self.assertEqual(line, b'')

    async def test_invalid_request_is_closed_without_logging_its_contents(self):
        loop = asyncio.get_running_loop()
        previous, errors = loop.get_exception_handler(), []
        loop.set_exception_handler(lambda current, context: errors.append(context))
        try:
            await self.send({'jsonrpc': '2.0', 'id': {'private': 'TEST_SECRET'}, 'method': 'ping'})
            self.assertEqual(await asyncio.wait_for(self.reader.readline(), 5), b'')
            await asyncio.sleep(0)
            self.assertEqual(errors, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_duplicate_json_keys_are_rejected_before_forwarding(self):
        self.writer.write(b'{"jsonrpc":"2.0","id":2,"id":3,"method":"ping"}\n')
        await self.writer.drain()
        self.assertEqual(await asyncio.wait_for(self.reader.readline(), 5), b'')

    async def test_legacy_unknown_request_does_not_log_request_contents(self):
        import mcp.types as types
        if hasattr(types, 'jsonrpc_message_adapter'):
            self.skipTest('legacy MCP request handling')
        with self.assertNoLogs(level='WARNING'):
            await self.send({'jsonrpc': '2.0', 'id': 2, 'method': 'server/discover',
                             'params': {'private': 'TEST_SECRET'}})
            response = await self.read()
        self.assertEqual(response['error']['code'], -32601)
        self.assertNotIn('TEST_SECRET', str(response))
        await self.send({'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
        response = await self.read()
        self.assertEqual(response['result'], {})
