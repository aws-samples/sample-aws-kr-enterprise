"""V2 review regression specifications; execution deferred by user request."""
import asyncio
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import (
    CLIConnectionError, KiroSDKClient, ProtocolError, ToolAnnotations, create_mcp_server, tool)
from kiro_agent_sdk.mcp import SdkMcpBridge
from kiro_agent_sdk.mcp_output_tracker import McpOutputTracker
from test_agentcore import HAS_STARLETTE


class ReviewV2CoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_annotation_deepcopy_and_tool_registration(self):
        for annotation in (ToolAnnotations(read_only_hint=True), ToolAnnotations(title="Probe")):
            duplicate = copy.deepcopy(annotation)
            self.assertEqual(duplicate, annotation)
            self.assertIsNot(duplicate, annotation)
            self.assertFalse(hasattr(annotation, "__setstate__"))
            @tool("probe", "Probe", {}, annotations=annotation)
            async def probe(arguments):
                return {"content": [{"type": "text", "text": "original"}]}
            bridge = SdkMcpBridge(create_mcp_server("review", tools=[probe]))
            try:
                listed = await bridge._dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual(listed["result"]["tools"][0]["name"], "probe")
                result = await bridge._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                "params": {"name": "probe", "arguments": {}}})
                self.assertNotIn("error", result)
                self.assertEqual(result["result"]["content"][0]["text"], "original")
            finally:
                await bridge.close()

    async def test_cancelling_non_tool_requests_releases_tracker_capacity(self):
        responses = []
        async def send(value):
            responses.append(value)
        tracker = McpOutputTracker(AsyncMock(), send, lambda: self.fail("Tracker failed"))
        for ident in range(140):
            await tracker.request({"jsonrpc": "2.0", "id": ident, "method": "resources/read",
                                   "params": {"uri": "file:///example"}})
            await tracker.cancel(ident)
        self.assertEqual(len(responses), 140)
        self.assertFalse(tracker._pending)
        self.assertTrue(all(value["error"]["code"] == -32800 for value in responses))

    async def test_known_rpc_requires_object_but_extension_preserves_null(self):
        client = KiroSDKClient()
        async def respond(message):
            client._pending[message["id"]].set_result(None)
        with patch.object(client, "_send", side_effect=respond):
            with self.assertRaisesRegex(ProtocolError, "object response"):
                await client._request("session/new", {})
            self.assertIsNone(await client._request("_kiro/custom", {}))
        self.assertFalse(client._pending)


class ReviewV2CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_callback_burst_queues_at_32_without_losing_requests(self):
        client = KiroSDKClient()
        release, full = asyncio.Event(), asyncio.Event()
        active = peak = completed = 0
        async def callback():
            nonlocal active, peak, completed
            active += 1
            peak = max(peak, active)
            if active == 32:
                full.set()
            try:
                await release.wait()
                completed += 1
            finally:
                active -= 1
        tasks = [asyncio.create_task(client._invoke_native_callback(callback)) for _ in range(40)]
        try:
            await asyncio.wait_for(full.wait(), 2)
            self.assertEqual(active, 32)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual((peak, completed), (32, 40))

    async def test_disconnect_lock_cancels_queued_owners_and_blocks_new_ones(self):
        from kiro_agent_sdk.loop_lock import LazyLoopLock
        lock = LazyLoopLock()
        started = asyncio.Event()
        async def holder():
            async with lock:
                started.set()
                await asyncio.Event().wait()
        first = asyncio.create_task(holder())
        await started.wait()
        second = asyncio.create_task(holder())
        await asyncio.sleep(0)
        lock.cancel_others({asyncio.current_task()})
        async with lock:
            self.assertIs(lock.owner, asyncio.current_task())
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(all(isinstance(value, CLIConnectionError) for value in results))
        self.assertFalse(first.cancelled())
        self.assertFalse(second.cancelled())


@unittest.skipUnless(HAS_STARLETTE, "requires optional AgentCore dependencies")
class ReviewV2AgentCoreTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        from kiro_agent_sdk.agentcore import _AgentCoreService, AgentCoreConfig
        return _AgentCoreService(AgentCoreConfig(allow_unauthorized_sessions=True, api_key="synthetic"), None, None)

    async def test_state_io_failure_after_cancellation_does_not_replace_cancel(self):
        service = self.service()
        started, release = threading.Event(), threading.Event()
        def worker():
            started.set()
            if not release.wait(5):
                raise TimeoutError("fixture was not released")
            raise ValueError("worker failed after caller cancellation")
        operation = asyncio.create_task(service._state_io(worker))
        try:
            await asyncio.wait_for(asyncio.to_thread(started.wait), 2)
            operation.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation
        finally:
            release.set()
            await asyncio.gather(operation, return_exceptions=True)

    async def test_pending_state_io_does_not_skip_other_session_cleanup(self):
        service = self.service()
        release = asyncio.Event()
        pending_io = asyncio.create_task(release.wait())
        service._state_io_operations.add(pending_io)
        pending_io.add_done_callback(service._state_io_operations.discard)
        login = SimpleNamespace(cancel=AsyncMock())
        service.sessions = {"one": SimpleNamespace(runtime_id="one", device_login=login)}
        original_wait = asyncio.wait
        async def bounded(tasks, **kwargs):
            return await original_wait(tasks, timeout=0)
        with patch.object(service, "_discard_client", new=AsyncMock()) as client, \
             patch.object(service, "_close_background_tasks", new=AsyncMock()) as background:
            try:
                with patch("kiro_agent_sdk.agentcore.asyncio.wait", side_effect=bounded):
                    with self.assertRaisesRegex(RuntimeError, "incomplete"):
                        await service.close()
                client.assert_awaited_once()
                background.assert_awaited_once()
                login.cancel.assert_awaited_once()
                self.assertIn("one", service.sessions)
            finally:
                release.set()
                await pending_io
            await service.close()
            self.assertFalse(service.sessions)
