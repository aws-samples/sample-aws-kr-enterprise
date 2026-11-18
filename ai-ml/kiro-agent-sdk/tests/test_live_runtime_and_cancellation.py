"""Opt-in real-Kiro checks for runtime policies and cancellation boundaries; these consume credits.

A case whose required model behaviour (for example a tool call) does not happen is skipped
(BLOCKED_PREREQUISITE), never reported as PASS.
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (CLIConnectionError, HookMatcher, KiroAgentOptions, KiroSDKClient, PermissionResultAllow,
                            ResultMessage, create_mcp_server, delete_session, tool)


def isolated_env(root: Path) -> dict:
    return {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}


@unittest.skipUnless(os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli"),
                     "set KIRO_SDK_INTEGRATION=1 with an authenticated Kiro CLI")
class LiveRuntimeAndCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiro-live-gap-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.env = isolated_env(self.root)
        self.sessions = []

    async def asyncTearDown(self):
        for session_id in self.sessions:
            try:
                await delete_session(session_id, cwd=self.cwd, env=self.env)
            except Exception:
                pass

    def track(self, client):
        if client.session_id:
            self.sessions.append(client.session_id)

    async def turn(self, client, prompt):
        await client.query(prompt)
        return [message async for message in client.receive_response()]

    def counting_server(self, calls):
        @tool("count", "Increment and return the call counter", {})
        async def count(args):
            calls.append(True)
            return {"content": [{"type": "text", "text": f"COUNT={len(calls)}"}]}
        return {"probe": create_mcp_server("probe", tools=[count])}

    async def test_runtime_max_turns_stops_before_the_next_model_call(self):
        calls = []
        options = KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3", tools=["@mcp"], turn_timeout=120,
                                   permission_mode="trustAll", runtime_compat_mode="v3", max_turns=2,
                                   mcp_servers=self.counting_server(calls))
        async with KiroSDKClient(options) as client:
            self.track(client)
            messages = await self.turn(client, "Call the probe count tool five times, one call at a time, "
                                               "then reply with the final counter. Use no other tools.")
            result = next(m for m in messages if isinstance(m, ResultMessage))
            if result.stop_reason != "max_turn_requests":
                self.skipTest("model finished within the turn limit; max_turns boundary not reached")
            # Each model turn issues at most one sequential tool call; the third model call is never started.
            self.assertEqual((result.is_error, result.model_turns), (True, 2))
            self.assertLessEqual(len(calls), 2)
            follow = next(m for m in await self.turn(client, "Reply with exactly OK. Use no tools.")
                          if isinstance(m, ResultMessage))
            self.assertFalse(follow.is_error)

    async def test_runtime_reported_usage_is_marked_and_not_converted_to_cost(self):
        options = KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3", tools=[], turn_timeout=120,
                                   runtime_compat_mode="v3", runtime_usage_mode="reported")
        async with KiroSDKClient(options) as client:
            self.track(client)
            first = next(m for m in await self.turn(client, "Reply with exactly OK.") if isinstance(m, ResultMessage))
            second = next(m for m in await self.turn(client, "Reply with exactly OK.") if isinstance(m, ResultMessage))
        for result in (first, second):
            self.assertFalse(result.is_error)
            report = result.kiro_model_usage
            self.assertIsInstance(report, dict)
            self.assertIsInstance(report["complete"], bool)
            self.assertTrue(report["records"])
            totals = report["totals"]
            self.assertTrue(all(value is None or (type(value) is int and value >= 0) for value in totals.values()))
            if report["complete"]:
                self.assertGreater(totals["inputTokens"], 0)

    async def test_interrupt_during_prompt_hook_prevents_prompt_dispatch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def hold(event, tool_id, context):
            entered.set()
            await release.wait()
            return {}
        options = KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3", tools=[], turn_timeout=120,
                                   hooks={"promptSubmit": [HookMatcher(hooks=[hold])]})
        async with KiroSDKClient(options) as client:
            self.track(client)
            original = client._send

            async def record(message):
                sent.append(message.get("method"))
                await original(message)
            client._send = record
            query = asyncio.create_task(client.query("Reply with exactly FIRST."))
            await asyncio.wait_for(entered.wait(), 30)
            await client.interrupt()
            release.set()
            try:
                await asyncio.wait_for(query, 30)
            except Exception:
                pass
            self.assertNotIn("session/prompt", sent)
            result = next(m for m in await self.turn(client, "Reply with exactly SECOND.")
                          if isinstance(m, ResultMessage))
            self.assertFalse(result.is_error)
            self.assertEqual(sent.count("session/prompt"), 1)

    async def test_permission_callback_cancelled_error_settles_the_request(self):
        target = self.cwd / "probe.txt"
        calls = []

        async def cancelled(name, data, context):
            calls.append(name)
            raise asyncio.CancelledError()
        options = KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3", tools=["write"], turn_timeout=120,
                                   kiro_permissions={"rules": [{"capability": "fs_write", "effect": "ask"}]},
                                   can_use_tool=cancelled)
        async with KiroSDKClient(options) as client:
            self.track(client)
            messages = await asyncio.wait_for(self.turn(client, "Write TEST to probe.txt with the write tool."), 180)
            if not calls:
                self.skipTest("model did not request the write permission")
            self.assertFalse(target.exists())
            self.assertTrue(any(isinstance(m, ResultMessage) for m in messages))
            result = next(m for m in await self.turn(client, "Reply with exactly OK. Use no tools.")
                          if isinstance(m, ResultMessage))
            self.assertFalse(result.is_error)

    async def test_disconnect_settles_receiver_without_cancelling_the_application_task(self):
        options = KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3", tools=[], turn_timeout=120)
        client = KiroSDKClient(options)
        try:
            await client.connect()
            self.track(client)
            await client.query("Write a detailed 3000-word essay about distributed systems without tools.")
            outcome = {}

            async def receive():
                try:
                    async for _ in client.receive_response():
                        pass
                    outcome["value"] = "completed"
                except CLIConnectionError:
                    outcome["value"] = "connection-error"
                outcome["cancelling"] = asyncio.current_task().cancelling()
            receiver = asyncio.create_task(receive())
            await asyncio.sleep(1)
            await client.disconnect()
            await asyncio.wait_for(receiver, 30)
            self.assertIn(outcome["value"], ("completed", "connection-error"))
            self.assertEqual(outcome["cancelling"], 0)
            self.assertFalse(receiver.cancelled())
            await client.connect()
            self.track(client)
            result = next(m for m in await self.turn(client, "Reply with exactly OK.") if isinstance(m, ResultMessage))
            self.assertFalse(result.is_error)
        finally:
            await client.disconnect()

    async def test_resistant_permission_callback_blocks_reconnect_until_cleanup_finishes(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def resistant(name, data, context):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return PermissionResultAllow()
        options = KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3", tools=["write"], turn_timeout=120,
                                   kiro_permissions={"rules": [{"capability": "fs_write", "effect": "ask"}]},
                                   can_use_tool=resistant)
        client = KiroSDKClient(options)
        try:
            await client.connect()
            self.track(client)
            await client.query("Write TEST to probe.txt with the write tool.")
            try:
                await asyncio.wait_for(entered.wait(), 120)
            except asyncio.TimeoutError:
                self.skipTest("model did not request the write permission")
            with self.assertRaises(CLIConnectionError):
                await client.disconnect()
            with self.assertRaises(CLIConnectionError):
                await client.connect()
            release.set()
            await asyncio.wait_for(finished.wait(), 10)
            await asyncio.sleep(0.1)
            await client.disconnect()
            await client.connect()
            self.track(client)
            result = next(m for m in await self.turn(client, "Reply with exactly OK. Use no tools.")
                          if isinstance(m, ResultMessage))
            self.assertFalse(result.is_error)
            self.assertFalse((self.cwd / "probe.txt").exists())
        finally:
            release.set()
            await client.disconnect()


if __name__ == "__main__":
    unittest.main()
