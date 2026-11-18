import asyncio
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, McpOAuthConfig
from test_mcp_oauth import OAuthFixture
from test_remote_mcp_toggle import TestMcpHttpServer

FAKE = str(Path(__file__).with_name('fake_kiro.py'))


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'remote relay requires optional mcp dependency')
class RemoteMcpOutputTransformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.started, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.calls = []

    async def asyncTearDown(self):
        self.release.set()
        self.tmp.cleanup()

    def options(self, kind, url):
        async def transform(server, name, arguments, result):
            self.calls.append((server, name, arguments, result['content'][0]['text']))
            if name == 'hold':
                self.started.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    await self.release.wait()
                self.finished.set()
            if name == 'fail':
                raise RuntimeError('PRIVATE_EXCEPTION')
            return {'content': [{'type': 'text', 'text': 'REPLACED_' + name}]}
        return KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine='v3',
            env={'KIRO_HOME': str(self.root / 'kiro')}, sdk_mcp_toggle_mode='sdk-all',
            mcp_servers={'probe': {'type': kind, 'url': url, 'headers': {'Authorization': 'Bearer fixture-secret'}}},
            sdk_mcp_output_transforms={'probe': transform})

    async def connect_bridge(self, bridge):
        reader, writer = await asyncio.open_connection('127.0.0.1', bridge.server.sockets[0].getsockname()[1])
        writer.write((bridge.token + '\n').encode())
        await self.send(writer, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                       'clientInfo': {'name': 'test', 'version': '1'}}})
        self.assertIn('result', await self.read(reader))
        await self.send(writer, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return reader, writer

    async def send(self, writer, wire):
        writer.write((json.dumps(wire) + '\n').encode())
        await writer.drain()

    async def call(self, writer, ident, name):
        await self.send(writer, {'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                                 'params': {'name': name, 'arguments': {}}})

    async def read(self, reader):
        line = await asyncio.wait_for(reader.readline(), 8)
        self.assertTrue(line, 'remote bridge closed unexpectedly')
        return json.loads(line)

    async def test_remote_results_replaced_and_callback_failure_is_sanitized(self):
        for kind in ('http', 'sse'):
            with self.subTest(kind=kind):
                service = TestMcpHttpServer(kind)
                url = await service.start()
                try:
                    async with KiroSDKClient(self.options(kind, url)) as client:
                        bridge = client._named_external_mcp_bridges['probe'][0]
                        reader, writer = await self.connect_bridge(bridge)
                        try:
                            await self.call(writer, 2, 'probe')
                            response = await self.read(reader)
                            self.assertEqual(response['result']['content'][0]['text'], 'REPLACED_probe')
                            await self.call(writer, 3, 'fail')
                            response = await self.read(reader)
                            self.assertTrue(response['result']['isError'])
                            self.assertNotIn('remote-result', str(response))
                            self.assertNotIn('PRIVATE_EXCEPTION', str(response))
                            self.assertFalse(service.bad_auth)
                        finally:
                            writer.close()
                            await writer.wait_closed()
                finally:
                    await service.close()

    async def test_remote_cancellation_suppresses_late_transformed_response(self):
        for kind in ('http', 'sse'):
            with self.subTest(kind=kind):
                self.started.clear(); self.release.clear(); self.finished.clear()
                service = TestMcpHttpServer(kind)
                url = await service.start()
                try:
                    async with KiroSDKClient(self.options(kind, url)) as client:
                        bridge = client._named_external_mcp_bridges['probe'][0]
                        reader, writer = await self.connect_bridge(bridge)
                        try:
                            await self.call(writer, 2, 'hold')
                            await asyncio.wait_for(self.started.wait(), 8)
                            await self.call(writer, 3, 'fast')
                            fast = await self.read(reader)
                            self.assertEqual((fast['id'], fast['result']['content'][0]['text']), (3, 'REPLACED_fast'))
                            await asyncio.wait_for(client.request_mcp_tool_cancellation('probe'), 8)
                            cancelled = await self.read(reader)
                            self.assertEqual((cancelled['id'], cancelled['error']['code']), (2, -32800))
                            self.release.set()
                            await asyncio.wait_for(self.finished.wait(), 3)
                            await self.send(writer, {'jsonrpc': '2.0', 'id': 4, 'method': 'ping'})
                            response = await self.read(reader)
                            self.assertEqual(response['id'], 4)
                        finally:
                            self.release.set()
                            writer.close()
                            await writer.wait_closed()
                finally:
                    await service.close()

    async def test_oauth_refresh_and_output_transform_work_together(self):
        for kind in ('http', 'sse'):
            with self.subTest(kind=kind):
                fixture = OAuthFixture(kind)
                url = await fixture.start()
                options = self.options(kind, url)
                options.mcp_servers['probe'].pop('headers')
                options.mcp_oauth = {'probe': McpOAuthConfig('http://127.0.0.1:9999/callback', fixture.authorize)}
                try:
                    async with KiroSDKClient(options) as client:
                        bridge = client._named_external_mcp_bridges['probe'][0]
                        reader, writer = await self.connect_bridge(bridge)
                        try:
                            await self.call(writer, 2, 'probe')
                            self.assertEqual((await self.read(reader))['result']['content'][0]['text'],
                                             'REPLACED_probe')
                            bridge.oauth.context.token_expiry_time = time.time() - 1
                            await self.call(writer, 3, 'probe')
                            self.assertEqual((await self.read(reader))['result']['content'][0]['text'],
                                             'REPLACED_probe')
                            self.assertEqual(fixture.token_grants, ['authorization_code', 'refresh_token'])
                        finally:
                            writer.close()
                            await writer.wait_closed()
                finally:
                    await fixture.close()

    async def test_inflight_remote_tool_cancellation_suppresses_late_result(self):
        for kind in ('http', 'sse'):
            with self.subTest(kind=kind):
                service = TestMcpHttpServer(kind)
                url = await service.start()
                try:
                    async with KiroSDKClient(self.options(kind, url)) as client:
                        bridge = client._named_external_mcp_bridges['probe'][0]
                        reader, writer = await self.connect_bridge(bridge)
                        try:
                            await self.call(writer, 2, 'slow')
                            await asyncio.wait_for(service.started.wait(), 8)
                            await asyncio.wait_for(client.request_mcp_tool_cancellation('probe'), 8)
                            response = await self.read(reader)
                            self.assertEqual((response['id'], response['error']['code']), (2, -32800))
                            service.release.set()
                            await self.send(writer, {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
                            response = await self.read(reader)
                            self.assertEqual(response['id'], 3)
                            self.assertFalse(any(call[1] == 'slow' for call in self.calls))
                        finally:
                            service.release.set()
                            writer.close()
                            await writer.wait_closed()
                finally:
                    await service.close()
