import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (KiroAgentOptions, KiroSDKClient, McpInputDeniedError,
                            create_mcp_server, tool)

FAKE = str(Path(__file__).with_name('fake_kiro.py'))


class SdkMcpInputTransformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.executed = []
        @tool('probe', 'Probe', {'value': str})
        async def probe(args):
            self.executed.append(args['value'])
            return {'content': [{'type': 'text', 'text': args['value']}]}
        self.server = create_mcp_server('probe', tools=[probe])

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def options(self, transform, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine='v3',
            env={'KIRO_HOME': str(self.root / 'kiro')}, mcp_servers={'probe': self.server},
            sdk_mcp_input_transforms={'probe': transform})
        values.update(changes)
        return KiroAgentOptions(**values)

    async def call(self, bridge, arguments=None):
        return await bridge._dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'probe', 'arguments': {'value': 'before'} if arguments is None else arguments}})

    async def test_input_is_copied_transformed_and_seen_by_output_transform(self):
        seen = []
        replacement = {'value': 'after'}
        async def transform(server, name, arguments):
            seen.append((server, name, arguments['value']))
            arguments['value'] = 'discarded'
            return replacement
        async def output(server, name, arguments, result):
            seen.append(arguments['value'])
            return result
        options = self.options(transform, sdk_mcp_output_transforms={'probe': output})
        async with KiroSDKClient(options) as client:
            response = await self.call(client._named_mcp_bridges['probe'][0])
            replacement['value'] = 'late'
        self.assertEqual((self.executed, seen), (['after'], [('probe', 'probe', 'before'), 'after']))
        self.assertEqual(response['result']['content'][0]['text'], 'after')

    async def test_denial_invalid_result_and_failure_never_execute_tool(self):
        async def deny(server, name, arguments):
            raise McpInputDeniedError('POLICY_REASON')
        async def invalid(server, name, arguments):
            return ['bad']
        async def fail(server, name, arguments):
            raise RuntimeError('PRIVATE_EXCEPTION')
        for callback, expected in ((deny, 'POLICY_REASON'), (invalid, 'SDK MCP input transform failed'),
                                   (fail, 'SDK MCP input transform failed')):
            with self.subTest(callback=callback):
                async with KiroSDKClient(self.options(callback)) as client:
                    response = await self.call(client._named_mcp_bridges['probe'][0])
                self.assertEqual(response['error']['message'], expected)
                self.assertNotIn('PRIVATE_EXCEPTION', str(response))
        self.assertEqual(self.executed, [])

    async def test_schema_validation_occurs_after_transformation(self):
        async def transform(server, name, arguments):
            return {'value': 12}
        async with KiroSDKClient(self.options(transform)) as client:
            response = await self.call(client._named_mcp_bridges['probe'][0])
        self.assertTrue(response['result']['isError'])
        self.assertEqual(self.executed, [])

    async def test_non_json_argument_types_are_rejected_before_handler(self):
        for replacement in ({'value': 'valid', 1: 'integer-key'}, {'value': ('tuple',)},
                            {'value': 'valid', 'nested': [{1: 'integer-key'}]},
                            {'value': 'valid', 'nested': float('nan')}):
            with self.subTest(replacement=replacement):
                async def transform(server, name, arguments):
                    return replacement
                async with KiroSDKClient(self.options(transform)) as client:
                    response = await self.call(client._named_mcp_bridges['probe'][0])
                    self.assertEqual(response['error']['message'], 'SDK MCP input transform failed')
        self.assertEqual(self.executed, [])

    async def test_cancelled_transform_cannot_execute_tool_late(self):
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def transform(server, name, arguments):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'value': 'late'}
        async with KiroSDKClient(self.options(transform)) as client:
            bridge = client._named_mcp_bridges['probe'][0]
            reader, writer = await asyncio.open_connection('127.0.0.1', bridge.server.sockets[0].getsockname()[1])
            try:
                request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'probe', 'arguments': {'value': 'before'}}}
                writer.write((bridge.token + '\n' + json.dumps(request) + '\n').encode())
                await writer.drain()
                await asyncio.wait_for(started.wait(), 2)
                writer.write((json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                          'params': {'requestId': 1}}) + '\n').encode())
                await writer.drain()
                response = json.loads(await asyncio.wait_for(reader.readline(), 2))
                self.assertEqual(response['error']['code'], -32800)
                release.set()
                await asyncio.wait_for(finished.wait(), 2)
                writer.write(b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
                await writer.drain()
                self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 2))['id'], 2)
            finally:
                release.set()
                writer.close()
                await writer.wait_closed()
        self.assertEqual(self.executed, [])

    async def test_disabling_during_input_transform_prevents_execution(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def transform(server, name, arguments):
            started.set()
            await release.wait()
            return arguments
        async with KiroSDKClient(self.options(transform)) as client:
            bridge = client._named_mcp_bridges['probe'][0]
            task = asyncio.create_task(self.call(bridge))
            await asyncio.wait_for(started.wait(), 2)
            bridge.set_tools_enabled(False)
            release.set()
            self.assertEqual((await task)['error']['code'], -32800)
        self.assertEqual(self.executed, [])

    async def test_external_transforms_require_explicit_mediation(self):
        async def transform(server, name, arguments):
            return arguments
        for config in ({'command': 'unused'}, {'type': 'http', 'url': 'https://example.invalid/mcp'},
                       {'type': 'sse', 'url': 'https://example.invalid/mcp'}):
            with self.subTest(config=config):
                options = self.options(transform, mcp_servers={'probe': config})
                with self.assertRaisesRegex(ValueError, 'explicitly mediated'):
                    await KiroSDKClient(options).connect()
