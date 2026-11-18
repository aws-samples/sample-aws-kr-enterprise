import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (CLIConnectionError, KiroAgentOptions, KiroSDKClient,
                            ProtocolError, create_mcp_server, tool)


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class SdkMcpToggleTests(unittest.IsolatedAsyncioTestCase):
    async def _connect_bridge(self, config):
        env = {entry["name"]: entry["value"] for entry in config["env"]}
        reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
        await writer.drain()
        return reader, writer

    async def _request(self, reader, writer, ident, method):
        payload = {"jsonrpc": "2.0", "id": ident, "method": method,
                   "params": {"name": "probe", "arguments": {}}}
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 2))

    async def test_toggle_cancels_existing_calls_and_rejects_active_turn(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        @tool("probe", "Wait", {})
        async def probe(args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-tools",
                                   mcp_servers={"server": create_mcp_server("server", tools=[probe])})
        async with KiroSDKClient(options) as client:
            reader, writer = await self._connect_bridge(client._mcp_servers[0])
            try:
                pending = asyncio.create_task(self._request(reader, writer, 1, "tools/call"))
                await asyncio.wait_for(started.wait(), 2)
                await client.toggle_mcp_server("server", False)
                self.assertEqual((await pending)["error"]["code"], -32800)
                await asyncio.wait_for(cancelled.wait(), 2)
                await client.query("cancel")
                with self.assertRaises(CLIConnectionError):
                    await client.toggle_mcp_server("server", True)
                await client.interrupt()
                _ = [message async for message in client.receive_response()]
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_explicit_cancellation_targets_sdk_tools_during_an_active_turn(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        @tool("probe", "Wait", {})
        async def probe(args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
            mcp_servers={"server": create_mcp_server("server", tools=[probe])})
        async with KiroSDKClient(options) as client:
            reader, writer = await self._connect_bridge(client._mcp_servers[0])
            try:
                pending = asyncio.create_task(self._request(reader, writer, 1, "tools/call"))
                await asyncio.wait_for(started.wait(), 3)
                await client.query("cancel")
                await client.request_mcp_tool_cancellation("server")
                self.assertEqual((await pending)["error"]["code"], -32800)
                await asyncio.wait_for(cancelled.wait(), 3)
                with self.assertRaisesRegex(ProtocolError, "SDK-managed"):
                    await client.request_mcp_tool_cancellation("unknown")
                await client.interrupt()
                _ = [message async for message in client.receive_response()]
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_uncertain_cancellation_aborts_client_without_waiting_for_uncooperative_callback(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
            mcp_servers={"server": create_mcp_server("server", tools=[])})
        client = KiroSDKClient(options)
        await client.connect()
        bridge = client._named_mcp_bridges["server"][0]
        entered, released = asyncio.Event(), asyncio.Event()
        async def uncooperative():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await released.wait()
        try:
            with patch.object(bridge, "request_tool_cancellation", side_effect=uncooperative):
                request_task = asyncio.create_task(client.request_mcp_tool_cancellation("server"))
                await asyncio.wait_for(entered.wait(), 3)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(request_task, 5)
                with self.assertRaises(CLIConnectionError):
                    await client.query("must not run")
        finally:
            released.set()
            await asyncio.sleep(0)
            await client.disconnect()

    async def test_explicit_opt_in_unknown_server_and_reconnect_are_required(self):
        server = create_mcp_server("server", tools=[])
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", mcp_servers={"server": server})
        async with KiroSDKClient(options) as client:
            with self.assertRaisesRegex(ProtocolError, "requires sdk_mcp_toggle_mode"):
                await client.toggle_mcp_server("server", False)
            options.sdk_mcp_toggle_mode = "sdk-tools"
            with self.assertRaisesRegex(ValueError, "Reconnect"):
                await client.query("hello")
        async with KiroSDKClient(options) as client:
            with self.assertRaisesRegex(ProtocolError, "Only SDK helper"):
                await client.toggle_mcp_server("unknown", False)
            with self.assertRaises(ValueError):
                await client.toggle_mcp_server("server", 1)

    async def test_disconnect_waits_for_toggle_and_failures_mark_connection_unusable(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-tools",
            mcp_servers={"server": create_mcp_server("server", tools=[])})
        client = KiroSDKClient(options)
        await client.connect()
        bridge = client._named_mcp_bridges["server"][0]
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed():
            entered.set()
            await release.wait()
        with patch.object(bridge, "cancel_tools", side_effect=delayed):
            toggle = asyncio.create_task(client.toggle_mcp_server("server", False))
            await asyncio.wait_for(entered.wait(), 2)
            close = asyncio.create_task(client.disconnect())
            await asyncio.sleep(0)
            self.assertFalse(close.done())
            await asyncio.wait_for(close, 2)
            with self.assertRaisesRegex(CLIConnectionError, "interrupted by disconnect"):
                await toggle
            self.assertFalse(toggle.cancelled())
            release.set()
        await client.connect()
        bridge = client._named_mcp_bridges["server"][0]
        try:
            with patch.object(bridge, "cancel_tools", side_effect=RuntimeError("failure")):
                with self.assertRaises(RuntimeError):
                    await client.toggle_mcp_server("server", False)
            with self.assertRaises(CLIConnectionError):
                await client.query("must-not-run")
        finally:
            await client.disconnect()

    async def test_official_mcp_call_already_dispatched_can_finish(self):
        try:
            import mcp.types as types
            from mcp.server import Server
        except ImportError:
            self.skipTest("optional mcp dependency not installed")
        started, release = asyncio.Event(), asyncio.Event()
        content = types.TextContent(type="text", text="ok")
        definition = types.Tool(name="probe", description="Probe", inputSchema={"type": "object"})
        if hasattr(types, "jsonrpc_message_adapter"):
            async def list_tools(ctx, params):
                return types.ListToolsResult(tools=[definition])
            async def call_tool(ctx, params):
                started.set()
                await release.wait()
                return types.CallToolResult(content=[content])
            server = Server("example", on_list_tools=list_tools, on_call_tool=call_tool)
        else:
            server = Server("example")
            @server.list_tools()
            async def list_tools():
                return [definition]
            @server.call_tool()
            async def call_tool(name, arguments):
                started.set()
                await release.wait()
                return [content]
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_mcp_toggle_mode="sdk-and-stdio",
            mcp_servers={"server": {"type": "sdk", "name": "server", "instance": server}})
        async with KiroSDKClient(options) as client:
            reader, writer = await self._connect_bridge(client._mcp_servers[0])
            try:
                init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}}}
                writer.write((json.dumps(init) + "\n").encode())
                await writer.drain()
                self.assertIn("result", json.loads(await asyncio.wait_for(reader.readline(), 5)))
                writer.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                await writer.drain()
                pending = asyncio.create_task(self._request(reader, writer, 2, "tools/call"))
                await asyncio.wait_for(started.wait(), 5)
                await client.toggle_mcp_server("server", False)
                release.set()
                self.assertEqual((await pending)["result"]["content"][0]["text"], "ok")
                self.assertEqual((await self._request(reader, writer, 3, "tools/call"))["error"]["code"], -32800)
            finally:
                release.set()
                writer.close()
                await writer.wait_closed()

    async def test_explicit_cancellation_requests_official_mcp_instance(self):
        try:
            import mcp.types as types
            from mcp.server import Server
        except ImportError:
            self.skipTest("optional mcp dependency not installed")
        started, cancelled = asyncio.Event(), asyncio.Event()
        definition = types.Tool(name="probe", description="Probe", inputSchema={"type": "object"})
        async def wait_for_cancellation():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        if hasattr(types, "jsonrpc_message_adapter"):
            async def list_tools(ctx, params):
                return types.ListToolsResult(tools=[definition])
            async def call_tool(ctx, params):
                await wait_for_cancellation()
            server = Server("example", on_list_tools=list_tools, on_call_tool=call_tool)
        else:
            server = Server("example")
            @server.list_tools()
            async def list_tools():
                return [definition]
            @server.call_tool()
            async def call_tool(name, arguments):
                await wait_for_cancellation()
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
            mcp_servers={"server": {"type": "sdk", "name": "server", "instance": server}})
        async with KiroSDKClient(options) as client:
            reader, writer = await self._connect_bridge(client._mcp_servers[0])
            pending = None
            try:
                init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}}}
                writer.write((json.dumps(init) + "\n").encode())
                await writer.drain()
                self.assertIn("result", json.loads(await asyncio.wait_for(reader.readline(), 5)))
                writer.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                await writer.drain()
                pending = asyncio.create_task(self._request(reader, writer, 2, "tools/call"))
                await asyncio.wait_for(started.wait(), 5)
                await client.request_mcp_tool_cancellation("server")
                await asyncio.wait_for(cancelled.wait(), 5)
            finally:
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                writer.close()
                await writer.wait_closed()

    async def test_interrupt_requests_cancellation_of_official_mcp_instance(self):
        try:
            import mcp.types as types
            from mcp.server import Server
        except ImportError:
            self.skipTest("optional mcp dependency not installed")
        started, cancelled = asyncio.Event(), asyncio.Event()
        definition = types.Tool(name="probe", description="Probe", inputSchema={"type": "object"})
        async def wait_for_cancellation():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        if hasattr(types, "jsonrpc_message_adapter"):
            async def list_tools(ctx, params):
                return types.ListToolsResult(tools=[definition])
            async def call_tool(ctx, params):
                await wait_for_cancellation()
            server = Server("example", on_list_tools=list_tools, on_call_tool=call_tool)
        else:
            server = Server("example")
            @server.list_tools()
            async def list_tools():
                return [definition]
            @server.call_tool()
            async def call_tool(name, arguments):
                await wait_for_cancellation()
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
            sdk_tool_interrupt_mode="cancel-observed",
            mcp_servers={"server": {"type": "sdk", "name": "server", "instance": server}})
        async with KiroSDKClient(options) as client:
            reader, writer = await self._connect_bridge(client._mcp_servers[0])
            pending = None
            try:
                init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}}}
                writer.write((json.dumps(init) + "\n").encode())
                await writer.drain()
                self.assertIn("result", json.loads(await asyncio.wait_for(reader.readline(), 5)))
                writer.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                await writer.drain()
                await client.query("cancel")
                pending = asyncio.create_task(self._request(reader, writer, 2, "tools/call"))
                await asyncio.wait_for(started.wait(), 5)
                await client.interrupt()
                await asyncio.wait_for(cancelled.wait(), 5)
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                self.assertEqual((await self._request(reader, writer, 3, "tools/call"))["error"]["code"], -32800)
                _ = [message async for message in client.receive_response()]
            finally:
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                writer.close()
                await writer.wait_closed()
