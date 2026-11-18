import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, create_mcp_server, tool

FAKE = str(Path(__file__).with_name('fake_kiro.py'))


class SdkMcpOutputTransformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        @tool('probe', 'A probe', {'value': str})
        async def probe(args):
            args['value'] = 'handler-mutated'
            return {'content': [{'type': 'text', 'text': 'ORIGINAL_SECRET'}]}
        self.server = create_mcp_server('probe', tools=[probe])

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def options(self, transform, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine='v3',
            env={'KIRO_HOME': str(self.root / 'kiro')}, mcp_servers={'probe': self.server},
            sdk_mcp_output_transforms={'probe': transform})
        values.update(changes)
        return KiroAgentOptions(**values)

    async def call(self, bridge):
        return await bridge._dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'probe', 'arguments': {'value': 'before'}}})

    async def test_transform_receives_original_arguments_and_replaces_tool_result(self):
        async def transform(server, name, arguments, result):
            self.calls.append((server, name, arguments['value'], result['content'][0]['text']))
            result['content'][0]['text'] = 'REPLACED'
            return result
        async with KiroSDKClient(self.options(transform)) as client:
            bridge = client._named_mcp_bridges['probe'][0]
            response = await self.call(bridge)
        self.assertEqual(self.calls, [('probe', 'probe', 'before', 'ORIGINAL_SECRET')])
        self.assertEqual(response['result']['content'][0]['text'], 'REPLACED')

    async def test_transform_result_is_copied_before_forwarding(self):
        replacement = {'content': [{'type': 'text', 'text': 'SAFE'}]}
        async def transform(server, name, arguments, result):
            return replacement
        async with KiroSDKClient(self.options(transform)) as client:
            response = await self.call(client._named_mcp_bridges['probe'][0])
            replacement['content'][0]['text'] = 'LATE_SECRET'
        self.assertEqual(response['result']['content'][0]['text'], 'SAFE')

    async def test_invalid_or_failing_transforms_do_not_disclose_original(self):
        for value in (None, {'content': 'bad'}, {'content': [{'type': 'text'}], 'isError': 'yes'},
                      {'content': [{'type': 'text', 'text': float('nan')}]}):
            async def transform(server, name, arguments, result):
                return value
            with self.subTest(value=value):
                async with KiroSDKClient(self.options(transform)) as client:
                    response = await self.call(client._named_mcp_bridges['probe'][0])
                self.assertTrue(response['result']['isError'])
                self.assertNotIn('ORIGINAL_SECRET', str(response))
        async def fail(server, name, arguments, result):
            raise RuntimeError('SECRET_EXCEPTION')
        async with KiroSDKClient(self.options(fail)) as client:
            response = await self.call(client._named_mcp_bridges['probe'][0])
        self.assertTrue(response['result']['isError'])
        self.assertNotIn('SECRET_EXCEPTION', str(response))
        self.assertNotIn('ORIGINAL_SECRET', str(response))

    async def test_timeout_does_not_wait_for_cancellation_resistant_callback(self):
        released, finished = asyncio.Event(), asyncio.Event()
        async def transform(server, name, arguments, result):
            try:
                await released.wait()
            except asyncio.CancelledError:
                await released.wait()
            finished.set()
            return result
        async with KiroSDKClient(self.options(transform, sdk_mcp_output_timeout=0.05)) as client:
            response = await asyncio.wait_for(self.call(client._named_mcp_bridges['probe'][0]), 1)
            self.assertTrue(response['result']['isError'])
            self.assertNotIn('ORIGINAL_SECRET', str(response))
            released.set()
            await asyncio.wait_for(finished.wait(), 1)

    async def test_cancelled_transform_cannot_send_a_late_result(self):
        started, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def transform(server, name, arguments, result):
            started.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                await released.wait()
            finished.set()
            return {'content': [{'type': 'text', 'text': 'LATE_SECRET'}]}
        async with KiroSDKClient(self.options(transform)) as client:
            bridge = client._named_mcp_bridges['probe'][0]
            port = bridge.server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            try:
                writer.write((bridge.token + '\n').encode())
                writer.write((json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'probe', 'arguments': {'value': 'before'}}}) + '\n').encode())
                await writer.drain()
                await asyncio.wait_for(started.wait(), 2)
                writer.write((json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                    'params': {'requestId': 1}}) + '\n').encode())
                await writer.drain()
                response = json.loads(await asyncio.wait_for(reader.readline(), 2))
                self.assertEqual(response['error']['code'], -32800)
                released.set()
                await asyncio.wait_for(finished.wait(), 2)
                writer.write((json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}) + '\n').encode())
                await writer.drain()
                response = json.loads(await asyncio.wait_for(reader.readline(), 2))
                self.assertEqual((response['id'], response['result']), (2, {}))
            finally:
                released.set()
                writer.close()
                await writer.wait_closed()

    async def test_configuration_is_named_and_immutable(self):
        async def transform(server, name, arguments, result):
            return result
        with self.assertRaises(ValueError):
            KiroSDKClient(self.options(transform, sdk_mcp_output_timeout=0))
        missing = KiroSDKClient(self.options(transform, sdk_mcp_output_transforms={'missing': transform}))
        with self.assertRaises(ValueError):
            await missing.connect()
        options = self.options(transform)
        async with KiroSDKClient(options) as client:
            options.sdk_mcp_output_transforms.clear()
            response = await self.call(client._named_mcp_bridges['probe'][0])
            self.assertTrue(response['result']['isError'])
            self.assertNotIn('ORIGINAL_SECRET', str(response))
            with self.assertRaises(ValueError):
                await client.query('must-not-run')

    async def test_callback_cannot_change_its_configuration_and_return_original(self):
        options = None
        async def transform(server, name, arguments, result):
            options.sdk_mcp_output_transforms.clear()
            return result
        options = self.options(transform)
        async with KiroSDKClient(options) as client:
            response = await self.call(client._named_mcp_bridges['probe'][0])
            self.assertTrue(response['result']['isError'])
            self.assertNotIn('ORIGINAL_SECRET', str(response))

    async def test_handler_error_is_transformed_and_stays_an_error(self):
        async def fail(args):
            raise ValueError('connect failed: password=SYNTHETIC_SECRET')
        self.server['tools'][0].handler = fail
        async def transform(server, name, arguments, result):
            self.calls.append(result)
            return {'content': [{'type': 'text', 'text': '[redacted]'}], 'isError': False}
        async with KiroSDKClient(self.options(transform)) as client:
            response = await self.call(client._named_mcp_bridges['probe'][0])
        self.assertTrue(self.calls[0]['isError'])
        self.assertIn('SYNTHETIC_SECRET', self.calls[0]['content'][0]['text'])
        self.assertEqual(response['result'], {'content': [{'type': 'text', 'text': '[redacted]'}], 'isError': True})

    async def test_error_result_from_handler_is_transformed(self):
        async def fail(args):
            return {'content': [{'type': 'text', 'text': 'SYNTHETIC_SECRET'}], 'is_error': True}
        self.server['tools'][0].handler = fail
        async def transform(server, name, arguments, result):
            return {'content': [{'type': 'text', 'text': '[redacted]'}]}
        async with KiroSDKClient(self.options(transform)) as client:
            response = await self.call(client._named_mcp_bridges['probe'][0])
        self.assertNotIn('SYNTHETIC_SECRET', str(response))
        self.assertTrue(response['result']['isError'])

    async def test_failing_transform_of_an_error_does_not_disclose_it(self):
        async def fail(args):
            raise ValueError('SYNTHETIC_SECRET')
        self.server['tools'][0].handler = fail
        async def transform(server, name, arguments, result):
            raise RuntimeError('transform failed')
        async with KiroSDKClient(self.options(transform)) as client:
            response = await self.call(client._named_mcp_bridges['probe'][0])
        self.assertNotIn('SYNTHETIC_SECRET', str(response))
        self.assertTrue(response['result']['isError'])

    async def test_invalid_tool_arguments_are_not_transformed(self):
        async def transform(server, name, arguments, result):
            self.fail('invalid tool calls must not be transformed')
        async with KiroSDKClient(self.options(transform)) as client:
            bridge = client._named_mcp_bridges['probe'][0]
            response = await bridge._dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                               'params': {'name': 'probe', 'arguments': {}}})
        self.assertTrue(response['result']['isError'])
        self.assertNotIn('original_args', str(response))
