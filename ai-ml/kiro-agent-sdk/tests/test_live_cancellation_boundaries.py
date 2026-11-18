"""Opt-in real-Kiro cancellation, cleanup and preparation-stage boundaries; these consume credits.

Each case uses a real Kiro session and a real local process, socket or file effect so the
boundary is observable outside the SDK. A required trigger that is not reached is skipped
(BLOCKED_PREREQUISITE), never reported as PASS.
"""
import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from kiro_agent_sdk import (CLIConnectionError, KiroAgentOptions, KiroSDKClient, ResultMessage, delete_session)
import kiro_agent_sdk.client as client_module


def isolated_env(root: Path) -> dict:
    return {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@unittest.skipUnless(os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli"),
                     "set KIRO_SDK_INTEGRATION=1 with an authenticated Kiro CLI")
class LiveCancellationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiro-live-cancel-")
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

    def options(self, **values):
        return KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3", tools=[], turn_timeout=120, **values)

    def track(self, client):
        if client.session_id and client.session_id not in self.sessions:
            self.sessions.append(client.session_id)

    def record_sends(self, client):
        sent = []
        original = client._send

        async def record(message):
            sent.append(message.get("method"))
            await original(message)
        client._send = record
        return sent

    async def turn(self, client, prompt):
        await client.query(prompt)
        return next(m for m in [m async for m in client.receive_response()] if isinstance(m, ResultMessage))

    async def test_concurrent_disconnect_settles_waiters_and_preserves_user_cancellation(self):
        client = KiroSDKClient(self.options())
        try:
            await client.connect()
            self.track(client)
            await client.query("Write a detailed 3000-word essay about compilers without tools.")
            outcomes = {}

            async def receive():
                try:
                    async for _ in client.receive_response():
                        pass
                    outcomes["receiver"] = "completed"
                except CLIConnectionError:
                    outcomes["receiver"] = "connection-error"
                outcomes["receiver_cancelling"] = asyncio.current_task().cancelling()

            async def control():
                try:
                    await client.get_context_usage()
                    outcomes["control"] = "completed"
                except CLIConnectionError:
                    outcomes["control"] = "connection-error"
                outcomes["control_cancelling"] = asyncio.current_task().cancelling()

            async def second_query():
                try:
                    await client.query("Reply with exactly LATE.")
                    outcomes["query"] = "sent"
                except CLIConnectionError:
                    outcomes["query"] = "connection-error"
                outcomes["query_cancelling"] = asyncio.current_task().cancelling()

            async def user_cancelled():
                await asyncio.Event().wait()
            receiver = asyncio.create_task(receive())
            await asyncio.sleep(1)
            waiters = [asyncio.create_task(control()), asyncio.create_task(second_query())]
            cancelled_by_user = asyncio.create_task(user_cancelled())
            await asyncio.sleep(.2)
            cancelled_by_user.cancel()
            await client.disconnect()
            await asyncio.wait_for(asyncio.gather(receiver, *waiters), 30)
            with self.assertRaises(asyncio.CancelledError):
                await cancelled_by_user
            self.assertIn(outcomes["receiver"], ("completed", "connection-error"))
            self.assertIn(outcomes["control"], ("completed", "connection-error"))
            self.assertIn(outcomes["query"], ("sent", "connection-error"))
            self.assertEqual((outcomes["receiver_cancelling"], outcomes["control_cancelling"],
                              outcomes["query_cancelling"]), (0, 0, 0))
            await client.connect()
            self.track(client)
            result = await self.turn(client, "Reply with exactly OK. Use no tools.")
            self.assertFalse(result.is_error)
        finally:
            await client.disconnect()

    async def test_one_failed_process_cleanup_is_reported_and_retried_before_reconnect(self):
        client = KiroSDKClient(self.options())
        original = client_module._terminate_owned_process
        failures = []

        async def fail_once(process, **kwargs):
            if not failures:
                failures.append(process.pid)
                raise CLIConnectionError("Subprocess cleanup did not finish")
            return await original(process, **kwargs)
        try:
            await client.connect()
            self.track(client)
            self.assertFalse((await self.turn(client, "Reply with exactly FIRST. Use no tools.")).is_error)
            pid = client._process.pid
            with patch.object(client_module, "_terminate_owned_process", side_effect=fail_once):
                with self.assertRaises(CLIConnectionError):
                    await client.disconnect()
                self.assertEqual(failures, [pid])
                if alive(pid):
                    # The failed cleanup must not be hidden by a reconnect.
                    with self.assertRaises(CLIConnectionError):
                        await client.connect()
                await client.disconnect()
            self.assertFalse(alive(pid))
            await client.connect()
            self.track(client)
            self.assertFalse((await self.turn(client, "Reply with exactly SECOND. Use no tools.")).is_error)
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()


if __name__ == "__main__":
    unittest.main()
