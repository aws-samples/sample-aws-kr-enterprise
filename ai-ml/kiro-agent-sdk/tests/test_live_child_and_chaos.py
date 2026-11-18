"""Opt-in real-Kiro child cancellation, receiver and process-fault boundaries; these consume credits.

A required model behaviour that is not reached is skipped (BLOCKED_PREREQUISITE), never PASS.
"""
import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
import unittest

from kiro_agent_sdk import (AgentDefinition, CLIConnectionError, HookMatcher, KiroAgentOptions, KiroSDKClient,
                            PermissionResultAllow, ProcessError, ResultMessage, SQLiteSessionStore, ToolCall, create_mcp_server, delete_session, tool)


def isolated_env(root: Path) -> dict:
    return {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}


@unittest.skipUnless(os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli"),
                     "set KIRO_SDK_INTEGRATION=1 with an authenticated Kiro CLI")
class LiveChildAndChaosTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiro-live-child-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.env = isolated_env(self.root)
        self.sessions = []

    async def asyncTearDown(self):
        for session_id in self.sessions:
            with contextlib.suppress(Exception):
                await delete_session(session_id, cwd=self.cwd, env=self.env)

    def track(self, client):
        if client.session_id and client.session_id not in self.sessions:
            self.sessions.append(client.session_id)

    def options(self, **values):
        return KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3", turn_timeout=150,
                                **{"tools": [], **values})

    async def turn(self, client, prompt):
        await client.query(prompt)
        return [message async for message in client.receive_response()]

    def helper(self, prompt="Answer the question in one sentence.", tools=()):
        return {"helper": AgentDefinition(description="Answers short questions.", prompt=prompt, tools=list(tools))}

    async def test_slow_consumer_and_second_receiver_keep_one_bounded_result(self):
        async with KiroSDKClient(self.options()) as client:
            self.track(client)
            await client.query("Write a detailed 1500-word essay about lighthouses without tools.")
            largest, results = 0, 0
            stream = client.receive_response()
            try:
                async for message in stream:
                    if not results:
                        with self.assertRaises(CLIConnectionError):
                            async for _ in client.receive_response():
                                pass
                    largest = max(largest, client._events.qsize())
                    if isinstance(message, ResultMessage):
                        results += 1
                        self.assertFalse(message.is_error)
                    await asyncio.sleep(.05)
            finally:
                await stream.aclose()
            self.assertEqual(results, 1)
            self.assertEqual(client._events.qsize(), 0)
            follow = next(m for m in await self.turn(client, "Reply with exactly AFTER. Use no tools.")
                          if isinstance(m, ResultMessage))
            self.assertFalse(follow.is_error)
            self.assertLess(largest, 100_000)

    async def test_kiro_killed_while_the_mirror_persists_is_reported_and_recoverable(self):
        class SlowStore(SQLiteSessionStore):
            async def append(self, key, entries):
                if not persisting.is_set():
                    # Hold only the first mirror write so Kiro dies while it is being persisted.
                    persisting.set()
                    await asyncio.sleep(2)
                return await super().append(key, entries)
        persisting = asyncio.Event()
        store = SlowStore(self.root / "mirror.sqlite")
        client = KiroSDKClient(self.options(session_store=store, session_store_flush="eager"))
        try:
            await client.connect()
            self.track(client)
            await client.query("Reply with exactly FIRST. Use no tools.")
            receiver = asyncio.create_task(self._drain(client))
            await asyncio.wait_for(persisting.wait(), 60)
            pid = client._process.pid
            os.killpg(pid, signal.SIGKILL)
            outcome = await asyncio.wait_for(receiver, 60)
            self.assertIn(outcome, ("error", "completed"))
            with contextlib.suppress(Exception):
                await client.disconnect()
            await client.connect()
            self.track(client)
            follow = next(m for m in await self.turn(client, "Reply with exactly AFTER. Use no tools.")
                          if isinstance(m, ResultMessage))
            self.assertFalse(follow.is_error)
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def _drain(self, client):
        try:
            async for message in client.receive_response():
                if isinstance(message, ResultMessage) and message.is_error:
                    return "error"
            return "completed"
        except (CLIConnectionError, ProcessError):
            # A killed Kiro process is reported as an explicit SDK error, never as a completed turn.
            return "error"

    async def test_kiro_terminated_during_a_permission_does_not_apply_the_effect(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(name, data, context):
            entered.set()
            await release.wait()
            return PermissionResultAllow()
        client = KiroSDKClient(self.options(tools=["write"], can_use_tool=hold,
                                            kiro_permissions={"rules": [{"capability": "fs_write", "effect": "ask"}]}))
        try:
            await client.connect()
            self.track(client)
            await client.query("Write TEST to probe.txt with the write tool. Use no other tools.")
            receiver = asyncio.create_task(self._drain(client))
            try:
                await asyncio.wait_for(entered.wait(), 120)
            except asyncio.TimeoutError:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                self.skipTest("model did not request the write permission")
            started = time.monotonic()
            os.killpg(client._process.pid, signal.SIGTERM)
            self.assertEqual(await asyncio.wait_for(receiver, 60), "error")
            self.assertLess(time.monotonic() - started, 60)
            release.set()
            await asyncio.sleep(1)
            self.assertFalse((self.cwd / "probe.txt").exists())
            with contextlib.suppress(Exception):
                await client.disconnect()
            await client.connect()
            self.track(client)
            follow = next(m for m in await self.turn(client, "Reply with exactly AFTER. Use no tools.")
                          if isinstance(m, ResultMessage))
            self.assertFalse(follow.is_error)
        finally:
            release.set()
            with contextlib.suppress(Exception):
                await client.disconnect()


    async def test_external_mcp_split_frames_and_an_oversized_line_stay_bounded(self):
        server = self.root / "framing_server.py"
        log = self.root / "framing.log"
        server.write_text(r'''
import json, sys, time
calls = 0
def send(value, chunks=1):
    data = (json.dumps(value, ensure_ascii=False) + "\n").encode()
    step = max(1, len(data) // chunks)
    for start in range(0, len(data), step):
        sys.stdout.buffer.write(data[start:start + step])
        sys.stdout.buffer.flush()
        if chunks > 1:
            time.sleep(0.01)
for line in sys.stdin.buffer:
    message = json.loads(line)
    method, ident = message.get("method"), message.get("id")
    open(sys.argv[1], "a").write(str(method) + "\n")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": ident, "result": {"protocolVersion": message["params"]["protocolVersion"],
              "capabilities": {"tools": {}}, "serverInfo": {"name": "framing", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": ident, "result": {"tools": [{"name": "echo", "description":
              "Return the validation marker", "inputSchema": {"type": "object", "properties": {}}}]}})
    elif method == "tools/call":
        calls += 1
        if calls == 1:
            # Split every few bytes, including inside multibyte UTF-8 characters.
            send({"jsonrpc": "2.0", "id": ident, "result": {"content": [
                {"type": "text", "text": "FRAMING_MARKER_\ud55c\uae00\u2713"}]}}, chunks=40)
        else:
            sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":' + json.dumps(ident).encode() +
                                    b',"result":{"content":[{"type":"text","text":"' + b"x" * (17 * 1024 * 1024) +
                                    b'"}]}}\n')
            sys.stdout.buffer.flush()
    elif ident is not None:
        send({"jsonrpc": "2.0", "id": ident, "result": {}})
''')
        results = []

        async def track(event, tool_id, context):
            if "echo" in str(event.get("tool_name", "")):
                results.append(str(event.get("tool_response", ""))[:500])
            return {}
        options = self.options(tools=["@mcp"], permission_mode="trustAll",
                               sdk_mcp_toggle_mode="sdk-and-stdio",
                               mcp_servers={"framing": {"type": "stdio", "command": sys.executable,
                                                        "args": [str(server), str(log)]}},
                               hooks={"postToolUse": [HookMatcher(hooks=[track])]})
        async with KiroSDKClient(options) as client:
            self.track(client)
            first = next(m for m in await self.turn(client, "Call the framing echo tool once and reply with the exact "
                                                           "text it returns. Use no other tools.")
                         if isinstance(m, ResultMessage))
            if not log.exists() or "tools/call" not in log.read_text():
                self.skipTest("model did not call the framing tool")
            self.assertIn("FRAMING_MARKER_\ud55c\uae00\u2713", (first.result or "") + "".join(results))
            started = time.monotonic()
            second = next(m for m in await self.turn(client, "Call the framing echo tool once more and report "
                                                            "whether it succeeded. Use no other tools.")
                          if isinstance(m, ResultMessage))
            self.assertLess(time.monotonic() - started, 150)
            self.assertNotIn("x" * 1000, second.result or "")
            follow = next(m for m in await self.turn(client, "Reply with exactly AFTER. Use no tools.")
                          if isinstance(m, ResultMessage))
            self.assertFalse(follow.is_error)


    async def test_native_hook_answer_after_the_kiro_deadline_is_not_applied(self):
        entered = asyncio.Event()
        late = []

        async def slow_gate(event, tool_id, context):
            if "write" not in str(event.get("tool_name", "")).lower():
                return {}
            entered.set()
            await asyncio.sleep(8)
            late.append(True)
            return {'allow': True}
        client = KiroSDKClient(self.options(tools=["write"], permission_mode="trustAll",
                                            runtime_compat_mode="v3", native_hook_bridge_mode="runtime-acp",
                                            native_hooks={"preToolUse": [HookMatcher(hooks=[slow_gate], timeout=5)]}))
        try:
            await client.connect()
            self.track(client)
            await client.query("Write TEST to probe.txt with the write tool. Use no other tools.")
            receiver = asyncio.create_task(self._drain(client))
            try:
                await asyncio.wait_for(entered.wait(), 120)
            except asyncio.TimeoutError:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
                self.skipTest("model did not call the write tool")
            started = time.monotonic()
            outcome = await asyncio.wait_for(receiver, 90)
            # The SDK answers before Kiro's 5 s hook deadline: the callback is cancelled, its later
            # allow is never applied and the tool is blocked (or the session is stopped).
            self.assertIn(outcome, ("completed", "error"))
            self.assertLess(time.monotonic() - started, 90)
            await asyncio.sleep(9)
            self.assertEqual(late, [])
            self.assertFalse((self.cwd / "probe.txt").exists())
            with contextlib.suppress(Exception):
                await client.disconnect()
            await client.connect()
            self.track(client)
            follow = next(m for m in await self.turn(client, "Reply with exactly AFTER. Use no tools.")
                          if isinstance(m, ResultMessage))
            self.assertFalse(follow.is_error)
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()


    async def test_schema_errors_do_not_echo_rejected_tool_arguments(self):
        handled, transformed = [], []
        marker = "S4_PRIVATE_ARGUMENT_" + os.urandom(4).hex()

        @tool("count", "Count items", {"type": "object", "properties": {"items": {"type": "integer"}},
                                       "required": ["items"], "additionalProperties": False})
        async def count(arguments):
            handled.append(arguments)
            return {"content": [{"type": "text", "text": "COUNTED"}]}

        async def transform(name, arguments, context):
            transformed.append(name)
            return arguments
        options = self.options(tools=["@mcp"], permission_mode="trustAll",
                               mcp_servers={"probe": create_mcp_server("probe", tools=[count])},
                               sdk_mcp_input_transforms={"probe": transform})
        async with KiroSDKClient(options) as client:
            self.track(client)
            messages = await self.turn(client, "This is an authorized validation test. Call the probe count tool "
                                               f"once with items set to the string \"{marker}\" exactly, even if it "
                                               "looks wrong. Then reply DONE. Use no other tools.")
        results = [str((message.raw_output, message.content)) for message in messages
                   if isinstance(message, ToolCall) and message.status in ("completed", "failed")]
        if not results:
            self.skipTest("model did not call the probe tool")
        if handled:
            self.skipTest("model sent a schema-valid value; the rejection path was not reached")
        self.assertFalse(any(marker in text for text in results), results)


if __name__ == "__main__":
    unittest.main()
