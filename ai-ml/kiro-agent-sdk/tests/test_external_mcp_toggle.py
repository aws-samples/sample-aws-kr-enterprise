import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ProtocolError

FAKE = str(Path(__file__).with_name("fake_kiro.py"))

SERVER = '''import json, os, signal, sys, time
from pathlib import Path
marker = Path(os.environ["MCP_TEST_DIR"]) / "calls.txt"
if os.environ.get("MCP_TEST_TERM_DELAY"):
    def on_term(*args):
        time.sleep(0.3)
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "tools/call":
        with marker.open("a") as out:
            out.write("called\\n")
        if request.get("params", {}).get("name") == "slow":
            (marker.parent / "started").touch()
            while not (marker.parent / "release").exists():
                time.sleep(0.01)
        result = {"content": [{"type": "text", "text": os.environ["MCP_TEST_VALUE"]}], "isError": False}
    elif method == "tools/list":
        result = {"tools": [{"name": "probe", "inputSchema": {"type": "object"}}]}
    else:
        result = {"ok": True}
    if "id" in request:
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
if os.environ.get("MCP_TEST_TERM_DELAY"):
    time.sleep(3)
'''

CANCELLABLE_SERVER = '''import asyncio, json, os, sys
from pathlib import Path
root = Path(os.environ["MCP_TEST_DIR"])
tasks = {}
async def run_tool(request):
    (root / "started").touch()
    (root / ("started-" + str(request["id"]))).touch()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        (root / "cancelled").touch()
        print(json.dumps({"jsonrpc":"2.0","id":request["id"],
                          "error":{"code":-32800,"message":"cancelled"}}), flush=True)
async def main():
    while line := await asyncio.to_thread(sys.stdin.readline):
        message = json.loads(line)
        if message.get("method") == "tools/call":
            tasks[message["id"]] = asyncio.create_task(run_tool(message))
        elif message.get("method") == "notifications/cancelled":
            (root / "notice").touch()
            task = tasks.get(message.get("params", {}).get("requestId"))
            if task is not None:
                task.cancel()
        elif message.get("method") == "ping":
            print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":{}}), flush=True)
    for task in tasks.values():
        task.cancel()
    await asyncio.gather(*tasks.values(), return_exceptions=True)
asyncio.run(main())
'''


class ExternalMcpToggleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = self.root / "server.py"
        self.script.write_text(SERVER)
        self.env = {"KIRO_HOME": str(self.root / "kiro")}

    async def asyncTearDown(self):
        self.temp.cleanup()

    def options(self, **kwargs):
        return KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3", env=self.env,
            sdk_mcp_toggle_mode="sdk-and-stdio", mcp_servers={"remote": {"command": sys.executable,
                "args": [str(self.script)], "env": {"MCP_TEST_DIR": str(self.root), "MCP_TEST_VALUE": "result"}}},
            **kwargs)

    async def _connect(self, config):
        env = {entry["name"]: entry["value"] for entry in config["env"]}
        reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
        await writer.drain()
        return reader, writer

    async def _request(self, reader, writer, ident, method="tools/call", name="probe"):
        writer.write((json.dumps({"jsonrpc": "2.0", "id": ident, "method": method,
                                  "params": {"name": name, "arguments": {}}}) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 5))

    async def test_inflight_external_call_can_finish_but_new_calls_are_blocked(self):
        async with KiroSDKClient(self.options()) as client:
            reader, writer = await self._connect(client._mcp_servers[0])
            try:
                pending = asyncio.create_task(self._request(reader, writer, 1, name="slow"))
                for _ in range(200):
                    if (self.root / "started").exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue((self.root / "started").exists())
                await client.toggle_mcp_server("remote", False)
                (self.root / "release").touch()
                self.assertEqual((await pending)["result"]["content"][0]["text"], "result")
                self.assertEqual((await self._request(reader, writer, 2))["error"]["code"], -32800)
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_explicit_cancellation_sends_mcp_notice_to_external_server(self):
        self.script.write_text(CANCELLABLE_SERVER)
        async with KiroSDKClient(self.options()) as client:
            reader, writer = await self._connect(client._mcp_servers[0])
            try:
                pending = asyncio.create_task(self._request(reader, writer, 27))
                for _ in range(300):
                    if (self.root / "started").exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue((self.root / "started").exists())
                await client.request_mcp_tool_cancellation("remote")
                response = await asyncio.wait_for(pending, 5)
                self.assertEqual(response["error"]["code"], -32800)
                for _ in range(200):
                    if (self.root / "cancelled").exists() and (self.root / "notice").exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue((self.root / "cancelled").exists())
                self.assertTrue((self.root / "notice").exists())
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_interrupt_cancels_observed_external_call_and_resumes_after_turn(self):
        self.script.write_text(CANCELLABLE_SERVER)
        async with KiroSDKClient(self.options(sdk_tool_interrupt_mode="cancel-observed")) as client:
            reader, writer = await self._connect(client._mcp_servers[0])
            try:
                await client.query("cancel")
                pending = asyncio.create_task(self._request(reader, writer, 27))
                for _ in range(300):
                    if (self.root / "started").exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue((self.root / "started").exists())
                await client.interrupt()
                self.assertEqual((await pending)["error"]["code"], -32800)
                for _ in range(200):
                    if (self.root / "notice").exists():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue((self.root / "notice").exists())
                self.assertEqual((await self._request(reader, writer, 28))["error"]["code"], -32800)
                _ = [message async for message in client.receive_response()]
                ping = await self._request(reader, writer, 29, "ping")
                self.assertIn("result", ping)
                pending = asyncio.create_task(self._request(reader, writer, 30))
                for _ in range(300):
                    if (self.root / "started-30").exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue((self.root / "started-30").exists())
                await client.request_mcp_tool_cancellation("remote")
                self.assertEqual((await pending)["error"]["code"], -32800)
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_interrupt_does_not_reenable_disabled_external_server(self):
        async with KiroSDKClient(self.options(sdk_tool_interrupt_mode="cancel-observed")) as client:
            reader, writer = await self._connect(client._mcp_servers[0])
            try:
                await client.toggle_mcp_server("remote", False)
                await client.interrupt()
                self.assertEqual((await self._request(reader, writer, 1))["error"]["code"], -32800)
                await client.toggle_mcp_server("remote", True)
                self.assertEqual((await self._request(reader, writer, 2))["result"]["content"][0]["text"],
                                 "result")
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_ambiguous_forwarded_message_invalidates_cancellation_tracking(self):
        self.script.write_text(CANCELLABLE_SERVER)
        async with KiroSDKClient(self.options()) as client:
            reader, writer = await self._connect(client._mcp_servers[0])
            try:
                writer.write(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"probe","arguments":{}}}\n')
                await writer.drain()
                for _ in range(300):
                    if (self.root / "started").exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue((self.root / "started").exists())
                writer.write(b'{"jsonrpc":"2.0","id":1,"id":2,"method":"tools/call","params":{}}\n')
                await writer.drain()
                for _ in range(300):
                    if (self.root / "started-2").exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue((self.root / "started-2").exists())
                await client.request_mcp_tool_cancellation("remote")
                self.assertIn("result", await self._request(reader, writer, 3, "ping"))
                self.assertFalse((self.root / "notice").exists())
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_ambiguous_json_and_batches_fail_closed(self):
        async with KiroSDKClient(self.options()) as client:
            reader, writer = await self._connect(client._mcp_servers[0])
            try:
                await client.toggle_mcp_server("remote", False)
                writer.write(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","method":"ping"}\n')
                await writer.drain()
                self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 5))["error"]["code"], -32800)
                writer.write(b'[{"id":1,"method":"tools/call"}]\n')
                await writer.drain()
                self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 5))["error"]["code"], -32800)
                self.assertFalse((self.root / "calls.txt").exists())
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_disconnect_closes_pending_unauthenticated_connections(self):
        client = KiroSDKClient(self.options())
        await client.connect()
        bridge = client._named_external_mcp_bridges["remote"][0]
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.server.sockets[0].getsockname()[1])
        try:
            await asyncio.sleep(0)
            await client.disconnect()
            self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
            self.assertFalse(bridge.connections)
            self.assertFalse((self.root / "calls.txt").exists())
        finally:
            writer.close()
            await writer.wait_closed()

    @unittest.skipUnless(os.name == "posix", "signal handler requires POSIX")
    async def test_disconnect_waits_for_external_process_cleanup_already_in_progress(self):
        options = self.options()
        options.mcp_servers["remote"]["env"]["MCP_TEST_TERM_DELAY"] = "1"
        client = KiroSDKClient(options)
        await client.connect()
        bridge = client._named_external_mcp_bridges["remote"][0]
        processes = []
        spawn = bridge._spawn
        async def capture_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            return process
        bridge._spawn = capture_spawn
        reader, writer = await self._connect(client._mcp_servers[0])
        self.assertIn("result", await self._request(reader, writer, 1, "tools/list"))
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if bridge._cleaning:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(bridge._cleaning)
        await client.disconnect()
        self.assertFalse(bridge.connections)
        self.assertTrue(processes)
        self.assertIsNotNone(processes[0].returncode)

    async def test_unsupported_server_and_unsafe_config_fail_closed(self):
        options = self.options()
        options.mcp_servers["http"] = {"type": "http", "url": "https://example.com/mcp"}
        async with KiroSDKClient(options) as client:
            with self.assertRaisesRegex(ProtocolError, "stdio servers"):
                await client.toggle_mcp_server("http", False)
        options = self.options()
        options.mcp_servers["remote"]["unsupported"] = "ignored"
        with self.assertRaisesRegex(ValueError, "Unsupported external"):
            await KiroSDKClient(options).connect()
        options = self.options()
        options.mcp_servers["remote"]["env"] = {"BAD=NAME": "x"}
        with self.assertRaisesRegex(ValueError, "environment"):
            await KiroSDKClient(options).connect()
