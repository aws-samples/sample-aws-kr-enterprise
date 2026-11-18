import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, create_mcp_server, tool


FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class SdkToolInterruptTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_observed_cancels_sdk_tool(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        @tool("probe", "Wait", {})
        async def probe(args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
            sdk_tool_interrupt_mode="cancel-observed",
            mcp_servers={"test": create_mcp_server("test", tools=[probe])})
        async with KiroSDKClient(options) as client:
            env = {entry["name"]: entry["value"] for entry in client._mcp_servers[0]["env"]}
            reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
            try:
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "probe", "arguments": {}}}
                writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n" + json.dumps(request) + "\n").encode())
                await writer.drain()
                await asyncio.wait_for(started.wait(), 2)
                await client.query("cancel")
                await client.interrupt()
                self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 2))["error"]["code"], -32800)
                await asyncio.wait_for(cancelled.wait(), 2)
                _ = [message async for message in client.receive_response()]
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_cancel_observed_failure_aborts_connection(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
            sdk_tool_interrupt_mode="cancel-observed",
            mcp_servers={"test": create_mcp_server("test", tools=[])})
        client = KiroSDKClient(options)
        await client.connect()
        try:
            bridge = client._mcp_bridges[0]
            with patch.object(bridge, "request_tool_cancellation", side_effect=RuntimeError("failure")):
                await client.query("cancel")
                with self.assertRaisesRegex(Exception, "could not be delivered"):
                    await client.interrupt()
            with self.assertRaises(Exception):
                await client.query("must not run")
        finally:
            await client.disconnect()

    async def test_cancel_observed_timeout_does_not_wait_for_uncooperative_bridge(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
            sdk_tool_interrupt_mode="cancel-observed",
            mcp_servers={"test": create_mcp_server("test", tools=[])})
        client = KiroSDKClient(options)
        await client.connect()
        entered, release = asyncio.Event(), asyncio.Event()
        async def uncooperative():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
        try:
            bridge = client._mcp_bridges[0]
            with patch.object(bridge, "request_tool_cancellation", side_effect=uncooperative):
                await client.query("cancel")
                interrupted = asyncio.create_task(client.interrupt())
                await asyncio.wait_for(entered.wait(), 2)
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(interrupted, 5)
            with self.assertRaises(Exception):
                await client.query("must not run")
        finally:
            release.set()
            await asyncio.sleep(0)
            await client.disconnect()

    async def test_cancel_all_cancels_sdk_tools_and_resumes_after_turn(self):
        started, cancelled, released, finished = (asyncio.Event() for _ in range(4))
        calls = 0

        @tool("probe", "Test", {})
        async def probe(args):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await released.wait()
                finished.set()
                text = "stale"
            else:
                text = "fresh"
            return {"content": [{"type": "text", "text": text}]}

        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_tool_interrupt_mode="cancel-all",
                                   mcp_servers={"test": create_mcp_server("test", tools=[probe])})
        async with KiroSDKClient(options) as client:
            env = {entry["name"]: entry["value"] for entry in client._mcp_servers[0]["env"]}
            reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
            writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())

            async def call(ident):
                message = {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                           "params": {"name": "probe", "arguments": {}}}
                writer.write((json.dumps(message) + "\n").encode())
                await writer.drain()

            try:
                await client.query("cancel")
                await call(1)
                await asyncio.wait_for(started.wait(), 2)
                await client.interrupt()
                await asyncio.wait_for(cancelled.wait(), 2)
                response = json.loads(await asyncio.wait_for(reader.readline(), 2))
                self.assertEqual((response["id"], response["error"]["code"]), (1, -32800))
                result = [message async for message in client.receive_response()][-1]
                self.assertEqual(result.stop_reason, "cancelled")
                released.set()
                await asyncio.wait_for(finished.wait(), 2)
                await call(2)
                response = json.loads(await asyncio.wait_for(reader.readline(), 2))
                self.assertEqual((response["id"], response["result"]["content"][0]["text"]), (2, "fresh"))
            finally:
                released.set()
                writer.close()
                await writer.wait_closed()

    async def test_cancel_all_rejects_official_mcp_instances(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_tool_interrupt_mode="cancel-all",
                                   mcp_servers={"test": {"type": "sdk", "name": "test", "instance": object()}})
        with self.assertRaisesRegex(ValueError, "MCP Server instances"):
            await KiroSDKClient(options).connect()

    async def test_cancel_all_includes_every_sdk_bridge_on_the_client(self):
        both_started = asyncio.Event()
        cancelled = []
        started = []
        @tool("probe", "Wait", {})
        async def probe(args):
            started.append(1)
            if len(started) == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(1)
                raise
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_tool_interrupt_mode="cancel-all",
            mcp_servers={"one": create_mcp_server("one", tools=[probe]),
                         "two": create_mcp_server("two", tools=[probe])})
        async with KiroSDKClient(options) as client:
            streams = []
            try:
                await client.query("cancel")
                for server in client._mcp_servers:
                    env = {entry["name"]: entry["value"] for entry in server["env"]}
                    reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
                    writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
                    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "probe", "arguments": {}}}
                    writer.write((json.dumps(request) + "\n").encode())
                    await writer.drain()
                    streams.append((reader, writer))
                await asyncio.wait_for(both_started.wait(), 2)
                await client.interrupt()
                replies = [json.loads(await asyncio.wait_for(reader.readline(), 2)) for reader, _ in streams]
                self.assertTrue(all(reply["error"]["code"] == -32800 for reply in replies))
                await asyncio.sleep(0)
                self.assertEqual(len(cancelled), 2)
                _ = [message async for message in client.receive_response()]
            finally:
                for _, writer in streams:
                    writer.close()
                    await writer.wait_closed()

    async def test_mode_change_requires_reconnect(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3")
        async with KiroSDKClient(options) as client:
            options.sdk_tool_interrupt_mode = "cancel-all"
            with self.assertRaisesRegex(ValueError, "Reconnect"):
                await client.query("not-sent")

    async def test_new_turn_waits_until_interrupt_cleanup_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_tool_interrupt_mode="cancel-all",
                                   mcp_servers={"test": create_mcp_server("test", tools=[])})
        async with KiroSDKClient(options) as client:
            bridge = client._mcp_bridges[0]
            async def delayed_cancel():
                entered.set()
                await release.wait()
            with patch.object(bridge, "cancel_tools", side_effect=delayed_cancel):
                await client.query("cancel")
                interrupt = asyncio.create_task(client.interrupt())
                await asyncio.wait_for(entered.wait(), 2)
                async def consume():
                    return [message async for message in client.receive_response()]
                receiver = asyncio.create_task(consume())
                await asyncio.sleep(0.05)
                self.assertFalse(receiver.done())
                with self.assertRaisesRegex(Exception, "interrupted response"):
                    await client.query("not-sent")
                release.set()
                await asyncio.wait_for(interrupt, 2)
                await asyncio.wait_for(receiver, 2)
            await client.query("ok")
            self.assertEqual([message async for message in client.receive_response()][-1].result, "ok")

    async def test_early_receiver_close_cancels_sdk_tools(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        @tool("probe", "Wait", {})
        async def probe(args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_tool_interrupt_mode="cancel-all",
                                   mcp_servers={"test": create_mcp_server("test", tools=[probe])})
        client = KiroSDKClient(options)
        await client.connect()
        env = {entry["name"]: entry["value"] for entry in client._mcp_servers[0]["env"]}
        reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
        try:
            await client.query("cancel")
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "probe", "arguments": {}}}
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            await asyncio.wait_for(started.wait(), 2)
            stream = client.receive_response()
            receiver = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(0)
            receiver.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await receiver
            await asyncio.wait_for(cancelled.wait(), 2)
            self.assertFalse(client._connected)
        finally:
            writer.close()
            await writer.wait_closed()
            await client.disconnect()

    async def test_failed_sdk_tool_cleanup_aborts_connection(self):
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_tool_interrupt_mode="cancel-all",
                                   mcp_servers={"test": create_mcp_server("test", tools=[])})
        async with KiroSDKClient(options) as client:
            bridge = client._mcp_bridges[0]
            async def fail():
                raise RuntimeError("synthetic bridge failure")
            with patch.object(bridge, "cancel_tools", side_effect=fail):
                await client.query("cancel")
                with self.assertRaisesRegex(RuntimeError, "synthetic bridge failure"):
                    await client.interrupt()
                try:
                    _ = [message async for message in client.receive_response()]
                except Exception:
                    pass
                self.assertFalse(client._connected)
                with self.assertRaises(Exception):
                    await client.query("not-sent")

    async def test_interrupt_without_active_turn_cancels_existing_sdk_calls(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        @tool("probe", "Wait", {})
        async def probe(args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        options = KiroAgentOptions(cli_path=FAKE, agent_engine="v3", sdk_tool_interrupt_mode="cancel-all",
                                   mcp_servers={"test": create_mcp_server("test", tools=[probe])})
        async with KiroSDKClient(options) as client:
            env = {entry["name"]: entry["value"] for entry in client._mcp_servers[0]["env"]}
            reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
            try:
                request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "probe", "arguments": {}}}
                writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n" + json.dumps(request) + "\n").encode())
                await writer.drain()
                await asyncio.wait_for(started.wait(), 2)
                await client.interrupt()
                await asyncio.wait_for(cancelled.wait(), 2)
                response = json.loads(await asyncio.wait_for(reader.readline(), 2))
                self.assertEqual(response["error"]["code"], -32800)
                self.assertFalse(client._sdk_tools_suspended)
            finally:
                writer.close()
                await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
