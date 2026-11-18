import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, McpInputDeniedError
from test_remote_mcp_toggle import TestMcpHttpServer, mcp

FAKE = str(Path(__file__).with_name('fake_kiro.py'))
STDIO_SERVER = '''import json, sys
from pathlib import Path
for line in sys.stdin:
    item = json.loads(line)
    if item.get("method") == "tools/call":
        with Path(sys.argv[1]).open("a") as f:
            f.write(json.dumps(item["params"]["arguments"]) + "\\n")
        result = {"content": [{"type": "text", "text": "result"}]}
    elif item.get("method") == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "probe", "version": "1"}}
    else:
        result = {}
    if "id" in item:
        print(json.dumps({"jsonrpc": "2.0", "id": item["id"], "result": result}), flush=True)
'''


@unittest.skipUnless(mcp is not None, 'optional MCP dependency')
class MediatedInputTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def connection(self, kind, transform, output=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            service = None
            if kind == 'instance':
                import mcp.types as types
                from mcp.server import Server
                tool_info = types.Tool(name='probe', description='Probe', inputSchema={
                    'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']})
                if hasattr(types, 'jsonrpc_message_adapter'):
                    async def list_tools(ctx, params):
                        return types.ListToolsResult(tools=[tool_info])
                    async def call_tool(ctx, params):
                        calls.append(params.arguments.copy())
                        return types.CallToolResult(content=[types.TextContent(type='text', text='result')])
                    server = Server('probe', on_list_tools=list_tools, on_call_tool=call_tool)
                else:
                    server = Server('probe')
                    @server.list_tools()
                    async def list_tools():
                        return [tool_info]
                    @server.call_tool()
                    async def call_tool(name, arguments):
                        calls.append(arguments.copy())
                        return types.CallToolResult(content=[types.TextContent(type='text', text='result')])
                config = {'type': 'sdk', 'name': 'probe', 'instance': server}
                mode = 'reject'
            elif kind == 'stdio':
                script = root / 'server.py'
                script.write_text(STDIO_SERVER)
                config = {'command': sys.executable, 'args': [str(script), str(root / 'calls')]}
                mode = 'sdk-and-stdio'
            else:
                service = TestMcpHttpServer(kind)
                url = await service.start()
                config = {'type': kind, 'url': url, 'headers': {'Authorization': 'Bearer fixture-secret'}}
                mode = 'sdk-all'
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine='v3',
                env={'KIRO_HOME': str(root / 'kiro')}, mcp_servers={'probe': config},
                sdk_mcp_toggle_mode=mode, sdk_mcp_input_transforms={'probe': transform},
                sdk_mcp_output_transforms={'probe': output} if output else {})
            try:
                async with KiroSDKClient(options) as client:
                    bridge = (client._named_mcp_bridges if kind == 'instance' else client._named_external_mcp_bridges)['probe'][0]
                    reader, writer = await asyncio.open_connection('127.0.0.1', bridge.server.sockets[0].getsockname()[1])
                    writer.write((bridge.token + '\n').encode())
                    await writer.drain()
                    try:
                        await self.send(writer, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                            'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                                       'clientInfo': {'name': 'test', 'version': '1'}}})
                        self.assertIn('result', await self.read(reader))
                        await self.send(writer, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
                        yield client, bridge, reader, writer, calls, service, root
                    finally:
                        writer.close()
                        await writer.wait_closed()
            finally:
                if service is not None:
                    await service.close()

    async def send(self, writer, message):
        writer.write((json.dumps(message) + '\n').encode())
        await writer.drain()

    async def read(self, reader):
        line = await asyncio.wait_for(reader.readline(), 8)
        self.assertTrue(line, 'bridge unexpectedly closed')
        return json.loads(line)

    async def call(self, writer, ident, value):
        await self.send(writer, {'jsonrpc': '2.0', 'id': ident, 'method': 'tools/call',
                                 'params': {'name': 'probe', 'arguments': {'value': value}}})

    def backend_calls(self, kind, calls, service, root):
        if kind == 'instance':
            return calls
        if kind == 'stdio':
            path = root / 'calls'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [item['params']['arguments'] for item in service.requests if item.get('method') == 'tools/call']

    async def test_rewrite_and_denial_for_all_transports(self):
        for kind in ('instance', 'stdio', 'http', 'sse'):
            with self.subTest(kind=kind):
                outputs = []
                async def transform(server, name, arguments):
                    if arguments['value'] == 'deny':
                        raise McpInputDeniedError('denied by policy')
                    return {'value': 'changed'}
                async def output(server, name, arguments, result):
                    outputs.append(arguments.copy())
                    return result
                async with self.connection(kind, transform, output) as (_, _, reader, writer, calls, service, root):
                    await self.call(writer, 2, 'before')
                    result = await self.read(reader)
                    self.assertIn('result', result)
                    await self.call(writer, 3, 'deny')
                    result = await self.read(reader)
                    self.assertEqual(result['error']['message'], 'denied by policy')
                    self.assertEqual(self.backend_calls(kind, calls, service, root), [{'value': 'changed'}])
                    self.assertEqual(outputs, [{'value': 'changed'}])

    async def test_cancel_before_send_cannot_execute_later_for_all_transports(self):
        for kind in ('instance', 'stdio', 'http', 'sse'):
            for cancel_mode in ('notification', 'sdk'):
                with self.subTest(kind=kind, cancel_mode=cancel_mode):
                    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
                    async def transform(server, name, arguments):
                        started.set()
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            await release.wait()
                        finished.set()
                        return {'value': 'late'}
                    try:
                        async with self.connection(kind, transform) as (_, bridge, reader, writer, calls, service, root):
                            await self.call(writer, 2, 'before')
                            await asyncio.wait_for(started.wait(), 8)
                            if cancel_mode == 'sdk':
                                await bridge.request_tool_cancellation()
                            else:
                                await self.send(writer, {'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                                         'params': {'requestId': 2}})
                            self.assertEqual((await self.read(reader))['error']['code'], -32800)
                            release.set()
                            await asyncio.wait_for(finished.wait(), 8)
                            await self.send(writer, {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
                            self.assertEqual((await self.read(reader))['id'], 3)
                            self.assertEqual(self.backend_calls(kind, calls, service, root), [])
                            if service is not None:
                                self.assertEqual(service.cancellations, [])
                    finally:
                        release.set()

    async def test_toggling_while_input_pending_prevents_forwarding(self):
        for kind in ('instance', 'stdio', 'http', 'sse'):
            with self.subTest(kind=kind):
                started, release = asyncio.Event(), asyncio.Event()
                async def transform(server, name, arguments):
                    started.set()
                    await release.wait()
                    return arguments
                try:
                    async with self.connection(kind, transform) as (_, bridge, reader, writer, calls, service, root):
                        await self.call(writer, 2, 'before')
                        await asyncio.wait_for(started.wait(), 8)
                        bridge.set_tools_enabled(False)
                        release.set()
                        self.assertEqual((await self.read(reader))['error']['code'], -32800)
                        self.assertEqual(self.backend_calls(kind, calls, service, root), [])
                finally:
                    release.set()

    async def test_cancelled_unsent_id_can_be_reused_safely(self):
        for kind in ('instance', 'stdio', 'http', 'sse'):
            with self.subTest(kind=kind):
                started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
                async def transform(server, name, arguments):
                    if arguments['value'] == 'old':
                        started.set()
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            await release.wait()
                        finished.set()
                        return {'value': 'old-late'}
                    return {'value': 'new'}
                try:
                    async with self.connection(kind, transform) as (_, _, reader, writer, calls, service, root):
                        await self.call(writer, 2, 'old')
                        await asyncio.wait_for(started.wait(), 8)
                        await self.send(writer, {'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                                 'params': {'requestId': 2}})
                        self.assertEqual((await self.read(reader))['error']['code'], -32800)
                        await self.call(writer, 2, 'new')
                        self.assertEqual((await self.read(reader))['id'], 2)
                        release.set()
                        await asyncio.wait_for(finished.wait(), 8)
                        await self.send(writer, {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'})
                        self.assertEqual((await self.read(reader))['id'], 3)
                        self.assertEqual(self.backend_calls(kind, calls, service, root), [{'value': 'new'}])
                finally:
                    release.set()

    async def test_bad_transform_fails_closed_and_does_not_expose_exception(self):
        for kind in ('instance', 'stdio', 'http', 'sse'):
            for invalid in (False, True):
                with self.subTest(kind=kind, invalid=invalid):
                    async def transform(server, name, arguments):
                        if invalid:
                            return ['not an object']
                        raise RuntimeError('PRIVATE_EXCEPTION')
                    async with self.connection(kind, transform) as (_, _, reader, writer, calls, service, root):
                        await self.call(writer, 2, 'before')
                        result = await self.read(reader)
                        self.assertEqual(result['error']['message'], 'SDK MCP input transform failed')
                        self.assertNotIn('PRIVATE_EXCEPTION', str(result))
                        self.assertEqual(self.backend_calls(kind, calls, service, root), [])

    async def test_duplicate_id_during_preflight_closes_connection_before_execution(self):
        for kind in ('instance', 'stdio', 'http', 'sse'):
            with self.subTest(kind=kind):
                started, release = asyncio.Event(), asyncio.Event()
                async def transform(server, name, arguments):
                    started.set()
                    await release.wait()
                    return arguments
                try:
                    async with self.connection(kind, transform) as (_, _, reader, writer, calls, service, root):
                        await self.call(writer, 2, 'before')
                        await asyncio.wait_for(started.wait(), 8)
                        await self.send(writer, {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
                        self.assertEqual(await asyncio.wait_for(reader.readline(), 8), b'')
                        release.set()
                        self.assertEqual(self.backend_calls(kind, calls, service, root), [])
                finally:
                    release.set()
