import asyncio
import copy
import unittest

from kiro_agent_sdk.mcp_input_gate import McpInputGate
from kiro_agent_sdk.mcp_output_tracker import McpOutputTracker


class McpCleanupGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_transform_cannot_send_after_cleanup_begins(self):
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent = []

        async def transform(name, arguments, result):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return result

        async def send(message):
            sent.append(message)

        tracker = McpOutputTracker(transform, send, lambda: None)
        wire = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "tool", "arguments": {}}}
        await tracker.request(wire)
        await tracker.response({"jsonrpc": "2.0", "id": 1,
                                "result": {"content": [{"type": "text", "text": "private"}]}})
        await asyncio.wait_for(started.wait(), 5)
        closing = asyncio.create_task(tracker.close())
        try:
            await asyncio.wait_for(cancelled.wait(), 5)
            release.set()
            await asyncio.wait_for(closing, 5)
            self.assertEqual(sent, [])
        finally:
            release.set()
            if not closing.done():
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)

    async def test_tool_call_error_passes_through_the_output_transform(self):
        sent = []

        async def send(message):
            sent.append(message)

        async def redact(name, arguments, result):
            return {"content": [{"type": "text", "text": "[redacted]"}], "isError": True}

        async def unchanged(name, arguments, result):
            return result

        error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "Upstream unavailable",
                                                      "data": {"authorization": "SYNTHETIC_SECRET"}}}
        reviewed = {"content": [{"type": "text", "text": "Upstream unavailable"}], "isError": True}
        for transform, expected in (
                (redact, {"jsonrpc": "2.0", "id": 1,
                          "result": {"content": [{"type": "text", "text": "[redacted]"}], "isError": True}}),
                # Unreviewed fields such as error.data are dropped even if the transform changes nothing.
                (unchanged, {"jsonrpc": "2.0", "id": 1, "result": reviewed}),
                # Without an output transform (input review only) the error is forwarded as it is.
                (None, error)):
            with self.subTest(transform=transform):
                sent.clear()
                tracker = McpOutputTracker(transform, send, lambda: None)
                await tracker.request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "tool", "arguments": {}}})
                await tracker.response(copy.deepcopy(error))
                await asyncio.wait_for(asyncio.gather(*tracker._jobs), 5)
                await tracker.close()
                self.assertEqual(sent, [expected])

    async def test_input_transform_cannot_forward_after_cleanup_begins(self):
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sent, forwarded = [], []

        async def transform(name, arguments):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return arguments

        async def send(message):
            sent.append(message)

        async def backend(message):
            forwarded.append(message)

        tracker = McpOutputTracker(None, send, lambda: None)
        gate = McpInputGate(transform, tracker, send, backend, asyncio.Lock(), lambda: True, lambda: None)
        await gate.submit({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "tool", "arguments": {}}})
        await asyncio.wait_for(started.wait(), 5)
        closing = asyncio.create_task(gate.close())
        try:
            await asyncio.wait_for(cancelled.wait(), 5)
            release.set()
            await asyncio.wait_for(closing, 5)
            self.assertEqual(forwarded, [])
            self.assertEqual(sent, [])
        finally:
            release.set()
            if not closing.done():
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
            await tracker.close()

    async def test_input_is_rechecked_after_tracker_registration(self):
        registered, release = asyncio.Event(), asyncio.Event()
        enabled = True
        sent, forwarded = [], []

        class DelayedTracker:
            def __init__(self):
                self.discarded = []
            async def reject_cancelled_id(self, wire):
                return False
            async def has_id(self, ident):
                return False
            async def request(self, wire):
                registered.set()
                await release.wait()
                return True
            async def discard(self, ident):
                self.discarded.append(ident)

        async def transform(name, arguments):
            return arguments

        async def send(message):
            sent.append(message)

        async def backend(message):
            forwarded.append(message)

        tracker = DelayedTracker()
        gate = McpInputGate(transform, tracker, send, backend, asyncio.Lock(), lambda: enabled, lambda: None)
        await gate.submit({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "tool", "arguments": {}}})
        await asyncio.wait_for(registered.wait(), 5)
        enabled = False
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(gate._jobs)), 5)
        self.assertEqual(forwarded, [])
        self.assertEqual(tracker.discarded, [1])
        self.assertEqual(sent[0]["error"]["code"], -32800)
        await gate.close()

    async def test_cleanup_during_tracker_registration_does_not_forward_later(self):
        registered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        forwarded = []

        class DelayedTracker:
            def __init__(self):
                self.discarded = []
            async def reject_cancelled_id(self, wire):
                return False
            async def has_id(self, ident):
                return False
            async def request(self, wire):
                registered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return True
            async def discard(self, ident):
                self.discarded.append(ident)

        async def transform(name, arguments):
            return arguments

        async def send(message):
            raise AssertionError("No response should be sent after cleanup")

        async def backend(message):
            forwarded.append(message)

        tracker = DelayedTracker()
        gate = McpInputGate(transform, tracker, send, backend, asyncio.Lock(), lambda: True, lambda: None)
        await gate.submit({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "tool", "arguments": {}}})
        await asyncio.wait_for(registered.wait(), 5)
        closing = asyncio.create_task(gate.close())
        try:
            await asyncio.wait_for(cancelled.wait(), 5)
            release.set()
            await asyncio.wait_for(closing, 5)
            self.assertEqual(forwarded, [])
            self.assertEqual(tracker.discarded, [1])
        finally:
            release.set()
            if not closing.done():
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
