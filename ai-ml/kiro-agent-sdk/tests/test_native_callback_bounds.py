"""Native ACP hook callbacks keep 32 active slots, 128 pending requests and Kiro's own deadline.

The fake ACP peer is a real subprocess, so requests cross OS pipes concurrently. A request that
would exceed a bound is refused instead of queued, and a request that waited for a slot is
answered before the deadline Kiro gave it, never later.
"""
import asyncio
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import CLIConnectionError, HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


@unittest.skipUnless(os.name == "posix", "native hooks require POSIX")
class NativeCallbackBoundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "node_modules/@kiro/agent/dist/server/acp-server.js"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"fixture")
        source.with_name("xhr-sync-worker.js").write_bytes(b"worker")
        self.source = source
        patcher = patch("kiro_agent_sdk.native_hook_runtime.patch_runtime", return_value=b"patched fixture")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.active = 0
        self.peak = 0
        self.entered = 0
        self.release = asyncio.Event()

    async def hold(self, data, *_):
        self.active += 1
        self.entered += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return {}

    def client(self):
        return KiroSDKClient(KiroAgentOptions(
            cwd=self.root, cli_path=FAKE, agent_engine="v3", native_hook_bridge_mode="runtime-acp",
            native_hook_runtime_path=self.source,
            native_hooks={"preToolUse": [HookMatcher(hooks=[self.hold], timeout=10)]},
            env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_NATIVE_ACP": "1"}))

    @staticmethod
    async def until(predicate, timeout):
        # Poll instead of sleeping a fixed time: a loaded host may need longer to deliver the flood.
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        return predicate()

    async def flood(self, client, count, timeout_ms):
        return await client._request("_kiro/test/native-hook-flood", {
            "sessionId": client.session_id, "count": count, "timeoutMs": timeout_ms})

    async def test_thirty_two_callbacks_run_and_the_rest_wait_within_the_original_deadline(self):
        for count in (31, 32, 33):
            with self.subTest(count=count):
                self.release.clear()
                self.peak = self.entered = 0
                async with self.client() as client:
                    flood = asyncio.create_task(self.flood(client, count, 6000))
                    self.assertTrue(await self.until(lambda: self.peak >= min(count, 32), 4))
                    await asyncio.sleep(.3)
                    self.assertEqual(self.peak, min(count, 32))
                    self.release.set()
                    replies = (await asyncio.wait_for(flood, 20))["replies"]
                self.assertEqual(len(replies), count)
                self.assertEqual(self.peak, min(count, 32))
                # Every reply arrives before the 6 s deadline Kiro gave each request.
                self.assertTrue(all(item["elapsed"] < 6 for item in replies.values()), replies)

    async def test_queued_callbacks_past_their_deadline_are_refused_not_approved(self):
        async with self.client() as client:
            started = time.monotonic()
            with self.assertRaises((ProtocolError, CLIConnectionError, ConnectionError)):
                # Nothing is released: every request must be answered or the session closed by its deadline.
                await asyncio.wait_for(self.flood(client, 40, 3000), 20)
            self.assertLess(time.monotonic() - started, 12)
            self.assertEqual(self.peak, 32)
            self.release.set()

    async def test_requests_beyond_128_pending_are_refused_individually(self):
        for count, refused in ((127, 0), (128, 0), (129, 1)):
            with self.subTest(count=count):
                self.release.clear()
                self.peak = self.entered = 0
                async with self.client() as client:
                    flood = asyncio.create_task(self.flood(client, count, 10000))
                    self.assertTrue(await self.until(lambda: self.entered >= 32, 5))
                    await asyncio.sleep(1)
                    self.assertLessEqual(len(client._inbound_requests), 128)
                    self.assertLessEqual(len(client._native_acp_dispatch_tasks), 128)
                    self.release.set()
                    replies = (await asyncio.wait_for(flood, 30))["replies"]
                    # The extra request is answered with an error; the session and the others continue.
                    self.assertIsNone(client._reader_error)
                self.assertEqual(len(replies), count)
                self.assertEqual(sum(item["error"] for item in replies.values()), refused)
                self.assertEqual(self.entered, count - refused)

    async def test_peer_killed_mid_flood_releases_every_slot_before_reconnect(self):
        client = self.client()
        await client.connect()
        try:
            flood = asyncio.create_task(self.flood(client, 64, 10000))
            self.assertTrue(await self.until(lambda: self.peak >= 32, 5))
            self.assertEqual(self.peak, 32)
            os.killpg(client._process.pid, signal.SIGKILL)
            with self.assertRaises(BaseException):
                await asyncio.wait_for(flood, 10)
            self.release.set()
            await asyncio.sleep(.2)
            await client.disconnect()
            self.assertEqual(self.active, 0)
            self.assertFalse(client._native_acp_dispatch_tasks)
            self.release.clear()
            self.peak = 0
            await client.connect()
            flood = asyncio.create_task(self.flood(client, 2, 2000))
            await asyncio.sleep(.3)
            self.release.set()
            self.assertEqual(len((await asyncio.wait_for(flood, 5))["replies"]), 2)
        finally:
            self.release.set()
            await client.disconnect()


if __name__ == "__main__":
    unittest.main()
