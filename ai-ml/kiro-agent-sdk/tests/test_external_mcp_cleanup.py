import asyncio
import contextlib
import gc
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import CLIConnectionError, KiroAgentOptions, KiroSDKClient
from kiro_agent_sdk.client import _launch_cleanup_tasks, _spawn_cli
from kiro_agent_sdk.external_mcp import ExternalStdioMcpBridge
from kiro_agent_sdk.mcp_input_gate import McpInputGate
from test_agentcore import FAKE


async def reap_test_process(process):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), 5)
    if process.stdin is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.stdin.wait_closed(), 5)
    await asyncio.sleep(0)


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux process state and process groups")
class ExternalMcpCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_pump_cleanup_consumes_errors_without_logging_private_details(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        reports = []
        loop.set_exception_handler(lambda current, context: reports.append(context))
        bridge = ExternalStdioMcpBridge({"command": sys.executable}, None, {}, _spawn_cli)

        async def fail():
            raise RuntimeError("private pump error")

        try:
            task = asyncio.create_task(fail())
            await asyncio.wait((task,))
            await bridge._settle_cleanup([task], timeout=1, cancel_pending=False, fail_on_exception=False)
            self.assertFalse(bridge._cleanup_failed)
            del task
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(reports, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_close_consumes_all_connection_errors_without_logging_private_details(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        reports = []
        loop.set_exception_handler(lambda current, context: reports.append(context))
        bridge = ExternalStdioMcpBridge({"command": sys.executable}, None, {}, _spawn_cli)

        async def fail(message):
            raise RuntimeError(message)

        try:
            first = asyncio.create_task(fail("private first error"))
            second = asyncio.create_task(fail("private second error"))
            await asyncio.wait((first, second))
            bridge.connections.update((first, second))
            with self.assertRaises(CLIConnectionError) as error:
                await bridge.close()
            self.assertNotIn("private", str(error.exception))
            bridge.connections.clear()
            del first, second
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(reports, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_cleanup_failure_is_reported_without_exposing_details(self):
        spawned = []

        async def spawn(*args, **kwargs):
            process = await _spawn_cli(*args, **kwargs)
            spawned.append(process)
            return process

        async def fail_cleanup(process, **kwargs):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise RuntimeError("private cleanup error")

        bridge = ExternalStdioMcpBridge(
            {"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]}, None, {}, spawn)
        config = await bridge.start()
        env = {entry["name"]: entry["value"] for entry in config["env"]}
        _, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        try:
            writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
            await writer.drain()
            async def ready():
                while not spawned:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(ready(), 5)
            with patch("kiro_agent_sdk.client._terminate_owned_process", side_effect=fail_cleanup):
                with self.assertRaises(CLIConnectionError) as error:
                    await asyncio.wait_for(bridge.close(), 10)
            self.assertNotIn("private cleanup error", str(error.exception))
            self.assertFalse(bridge.connections)
            self.assertFalse(bridge._tools_enabled)
            self.assertTrue(bridge._closed)
            with self.assertRaises(CLIConnectionError):
                bridge.set_tools_enabled(True)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if spawned:
                await reap_test_process(spawned[0])
            with contextlib.suppress(Exception):
                await bridge.close()

    async def test_client_closes_remaining_bridges_after_one_cleanup_failure(self):
        class Bridge:
            def __init__(self, fail=False):
                self.fail, self.closed = fail, False
            async def close(self):
                self.closed = True
                if self.fail:
                    raise CLIConnectionError("cleanup failed")
        first, second = Bridge(fail=True), Bridge()
        client = KiroSDKClient(KiroAgentOptions())
        client._connected = True
        client._external_mcp_bridges = [first, second]
        with self.assertRaises(CLIConnectionError):
            await client.disconnect()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertFalse(client._connected)

    async def test_failed_external_bridge_cannot_partially_reenable_other_bridges(self):
        class OtherBridge:
            instance = None
            enabled = False
            def set_tools_enabled(self, enabled):
                self.enabled = enabled
        other = OtherBridge()
        external = ExternalStdioMcpBridge({"command": sys.executable}, None, {}, _spawn_cli)
        external._mark_cleanup_failed()
        client = KiroSDKClient(KiroAgentOptions(sdk_mcp_toggle_mode="sdk-and-stdio"))
        client._connected, client.session_id = True, "session"
        client._sdk_mcp_toggle_mode = "sdk-and-stdio"
        client._named_mcp_bridges = {"remote": [other]}
        client._named_external_mcp_bridges = {"remote": [external]}
        with self.assertRaises(CLIConnectionError):
            await client.toggle_mcp_server("remote", True)
        self.assertFalse(other.enabled)

    async def test_unexpected_bridge_cleanup_failure_invalidates_parent_session(self):
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cwd=root, cli_path=str(FAKE), agent_engine="v3",
                env={"KIRO_HOME": str(Path(root) / "kiro")}, sdk_mcp_toggle_mode="sdk-and-stdio",
                mcp_servers={"remote": {"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]}})
            client = KiroSDKClient(options)
            await client.connect()
            bridge = client._named_external_mcp_bridges["remote"][0]
            config = client._mcp_servers[0]
            env = {entry["name"]: entry["value"] for entry in config["env"]}
            _, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))

            async def fail_cleanup(process, **kwargs):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                raise RuntimeError("private cleanup error")

            try:
                writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
                await writer.drain()
                async def ready():
                    while not bridge.connections or not bridge._tool_cancellers:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(ready(), 5)
                with patch("kiro_agent_sdk.client._terminate_owned_process", side_effect=fail_cleanup):
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
                    async def failed():
                        while not bridge._cleanup_failed:
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(failed(), 5)
                self.assertIsInstance(client._reader_error, CLIConnectionError)
                self.assertNotIn("private cleanup error", str(client._reader_error))
                with self.assertRaises(CLIConnectionError):
                    await client.query("must not run")
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                with contextlib.suppress(CLIConnectionError):
                    await client.disconnect()

    async def test_repeated_cancellation_reports_incomplete_cleanup_and_releases_connection(self):
        spawned, entered = [], asyncio.Event()
        hold = asyncio.Event()

        async def spawn(*args, **kwargs):
            process = await _spawn_cli(*args, **kwargs)
            spawned.append(process)
            return process

        async def interrupted_cleanup(process, **kwargs):
            entered.set()
            try:
                await hold.wait()
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                raise

        bridge = ExternalStdioMcpBridge(
            {"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]}, None, {}, spawn)
        config = await bridge.start()
        env = {entry["name"]: entry["value"] for entry in config["env"]}
        _, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        closing = None
        try:
            writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
            await writer.drain()
            async def ready():
                while not spawned or not bridge.connections:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(ready(), 5)
            owner = next(iter(bridge.connections))
            with patch("kiro_agent_sdk.client._terminate_owned_process", side_effect=interrupted_cleanup):
                closing = asyncio.create_task(bridge.close())
                await asyncio.wait_for(entered.wait(), 5)
                owner.cancel()
                with self.assertRaises(CLIConnectionError):
                    await asyncio.wait_for(closing, 10)
            self.assertFalse(bridge.connections)
            self.assertFalse(bridge._tools_enabled)
        finally:
            hold.set()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if spawned:
                await reap_test_process(spawned[0])
            if closing is not None and not closing.done():
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
            with contextlib.suppress(Exception):
                await bridge.close()

    async def test_stuck_adapter_cleanup_does_not_keep_backend_process_alive(self):
        spawned, entered, release = [], asyncio.Event(), asyncio.Event()
        before = set(_launch_cleanup_tasks)

        async def spawn(*args, **kwargs):
            process = await _spawn_cli(*args, **kwargs)
            spawned.append(process)
            return process

        async def transform(name, arguments):
            return arguments

        async def blocked_close(gate):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        bridge = ExternalStdioMcpBridge(
            {"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]},
            None, {}, spawn, input_transform=transform)
        config = await bridge.start()
        env = {entry["name"]: entry["value"] for entry in config["env"]}
        _, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        try:
            writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
            await writer.drain()
            async def ready():
                while not spawned or not bridge._tool_cancellers:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(ready(), 5)
            with patch.object(McpInputGate, "close", blocked_close), \
                    patch("kiro_agent_sdk.external_mcp._MCP_CLEANUP_TIMEOUT", 0.2):
                closing = asyncio.create_task(bridge.close())
                await asyncio.wait_for(entered.wait(), 5)
                with self.assertRaises(CLIConnectionError):
                    await asyncio.wait_for(closing, 10)
            self.assertIsNotNone(spawned[0].returncode)
            self.assertFalse(bridge.connections)
            pending = {item for item in _launch_cleanup_tasks - before if not item.done()}
            self.assertGreaterEqual(len(pending), 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
        finally:
            release.set()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if spawned:
                await reap_test_process(spawned[0])
            with contextlib.suppress(Exception):
                await bridge.close()

    async def test_close_times_out_during_spawn_and_tracks_eventual_cleanup(self):
        entered, release = asyncio.Event(), asyncio.Event()
        spawned = []
        create = asyncio.create_subprocess_exec

        async def delayed_create(*args, **kwargs):
            entered.set()
            await release.wait()
            process = await create(*args, **kwargs)
            spawned.append(process)
            return process

        bridge = ExternalStdioMcpBridge(
            {"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]}, None, {}, _spawn_cli)
        config = await bridge.start()
        env = {entry["name"]: entry["value"] for entry in config["env"]}
        _, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
        owner = None
        try:
            with patch("kiro_agent_sdk.client.asyncio.create_subprocess_exec", side_effect=delayed_create), \
                    patch("kiro_agent_sdk.external_mcp._MCP_BRIDGE_CLOSE_TIMEOUT", 0.1):
                writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
                await writer.drain()
                await asyncio.wait_for(entered.wait(), 5)
                owner = next(iter(bridge.connections))
                with self.assertRaises(CLIConnectionError):
                    await asyncio.wait_for(bridge.close(), 5)
                self.assertFalse(owner.done())
                self.assertIn(owner, _launch_cleanup_tasks)
                release.set()
                await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 5)
            self.assertEqual(len(spawned), 1)
            self.assertIsNotNone(spawned[0].returncode)
        finally:
            release.set()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if spawned:
                await reap_test_process(spawned[0])
            if owner is not None and not owner.done():
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
            with contextlib.suppress(Exception):
                await bridge.close()

    async def test_close_terminates_descendant_that_holds_a_full_input_pipe(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "ready"
            script = (
                "import os,signal,sys,time\n"
                "pid=os.fork()\n"
                "if pid == 0:\n"
                " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                " open(sys.argv[1],'w').write(str(os.getpid()))\n"
                " os.close(1); time.sleep(60)\n"
                "else:\n"
                " time.sleep(60)\n")
            spawned = []

            async def spawn(*args, **kwargs):
                process = await _spawn_cli(*args, **kwargs)
                spawned.append(process)
                return process

            bridge = ExternalStdioMcpBridge(
                {"command": sys.executable, "args": ["-c", script, str(marker)]}, root, {}, spawn)
            config = await bridge.start()
            env = {entry["name"]: entry["value"] for entry in config["env"]}
            reader, writer = await asyncio.open_connection("127.0.0.1", int(env["KIRO_SDK_MCP_PORT"]))
            try:
                writer.write((env["KIRO_SDK_MCP_TOKEN"] + "\n").encode())
                await writer.drain()
                async def ready():
                    while not marker.exists() or not marker.read_text():
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(ready(), 5)
                child_pid = int(marker.read_text())
                writer.write(b"x" * (2 * 1024 * 1024) + b"\n")
                async def full_pipe():
                    while spawned[0].stdin.transport.get_write_buffer_size() == 0:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(full_pipe(), 5)
                await asyncio.wait_for(bridge.close(), 12)
                stat = Path(f"/proc/{child_pid}/stat")
                async def wait_for_child_exit():
                    while stat.exists():
                        try:
                            if stat.read_text().split()[2] == "Z":
                                return
                        except FileNotFoundError:
                            return
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_child_exit(), 5)
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                if spawned:
                    await reap_test_process(spawned[0])
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(bridge.close(), 10)
