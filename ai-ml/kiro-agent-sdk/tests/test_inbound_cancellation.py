import asyncio
import json
import unittest

from kiro_agent_sdk import (CLIConnectionError, KiroAgentOptions, KiroSDKClient, PermissionResultAllow,
                            PermissionResultDeny, Transport)


class PermissionTransport(Transport):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.messages = []
        self.ready = False
        self.prompt_id = None
        self.cancelled = False

    async def connect(self):
        self.ready = True

    async def write(self, data):
        message = json.loads(data)
        self.messages.append(message)
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
        elif method == "session/new":
            result = {"sessionId": "s", "modes": {"currentModeId": "vibe"}}
        elif method == "session/prompt":
            text = message["params"]["prompt"][0]["text"]
            if text in ("first", "second-permission"):
                self.cancelled = False
                self.prompt_id = message["id"]
                await self.queue.put({"jsonrpc": "2.0", "id": 100, "method": "session/request_permission",
                    "params": {"sessionId": "s", "toolCall": {"toolCallId": "tool", "title": "write",
                    "rawInput": {"path": "x"}}, "options": [{"optionId": "yes", "kind": "allow_once"},
                                                               {"optionId": "no", "kind": "reject_once"}]}})
                return
            await self.queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})
            result = {"stopReason": "end_turn"}
        elif method == "session/cancel":
            self.cancelled = True
            return
        elif method is None and message.get("id") == 100:
            await self.queue.put({"jsonrpc": "2.0", "id": self.prompt_id,
                                  "result": {"stopReason": "cancelled" if self.cancelled else "end_turn"}})
            return
        else:
            result = {}
        if "id" in message:
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": result})

    async def read_messages(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def close(self):
        self.ready = False
        await self.queue.put(None)

    def is_ready(self):
        return self.ready

    async def end_input(self):
        pass


class InteractionTransport(PermissionTransport):
    def __init__(self):
        super().__init__()
        self.response_ids = set()

    async def write(self, data):
        message = json.loads(data)
        if message.get("method") == "session/prompt":
            self.messages.append(message)
            self.prompt_id = message["id"]
            await self.queue.put({"jsonrpc": "2.0", "id": 101, "method": "_kiro/userInput",
                                  "params": {"sessionId": "s", "question": "Choose", "options": []}})
            await self.queue.put({"jsonrpc": "2.0", "id": 102, "method": "_kiro/mcp/elicitation",
                                  "params": {"sessionId": "s", "elicitation": {}}})
            return
        if message.get("method") is None and message.get("id") in (101, 102):
            self.messages.append(message)
            self.response_ids.add(message["id"])
            if self.response_ids == {101, 102}:
                await self.queue.put({"jsonrpc": "2.0", "id": self.prompt_id,
                                      "result": {"stopReason": "cancelled"}})
            return
        await super().write(data)


class OtherSessionTransport(PermissionTransport):
    async def write(self, data):
        message = json.loads(data)
        if message.get("method") == "session/prompt":
            self.messages.append(message)
            self.prompt_id = message["id"]
            await self.queue.put({"jsonrpc": "2.0", "id": 103, "method": "session/request_permission",
                "params": {"sessionId": "other", "toolCall": {"toolCallId": "tool", "title": "read",
                "rawInput": {}}, "options": [{"optionId": "yes", "kind": "allow_once"}]}})
            return
        if message.get("method") == "session/cancel":
            self.messages.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": self.prompt_id, "result": {"stopReason": "cancelled"}})
            return
        if message.get("method") is None and message.get("id") == 103:
            self.messages.append(message)
            return
        await super().write(data)


class FailedCancelTransport(PermissionTransport):
    async def write(self, data):
        if json.loads(data).get("method") == "session/cancel":
            raise OSError("synthetic cancellation write failure")
        await super().write(data)


class FailedInboundReplyTransport(PermissionTransport):
    async def write(self, data):
        message = json.loads(data)
        if message.get("method") is None and message.get("id") == 100:
            raise OSError("synthetic inbound reply failure")
        await super().write(data)


class SlowInboundReplyTransport(PermissionTransport):
    def __init__(self):
        super().__init__()
        self.reply_started = asyncio.Event()

    async def write(self, data):
        message = json.loads(data)
        if message.get("method") is None and message.get("id") == 100:
            self.reply_started.set()
            await asyncio.Event().wait()
        await super().write(data)


class ReuseDuringInterruptTransport(PermissionTransport):
    def __init__(self):
        super().__init__()
        self.replies = 0
        self.cancel_release = asyncio.Event()

    async def write(self, data):
        message = json.loads(data)
        if message.get("method") is None and message.get("id") == 100:
            self.messages.append(message)
            self.replies += 1
            if self.replies == 1:
                await self.queue.put({"jsonrpc": "2.0", "id": 100, "method": "session/request_permission",
                    "params": {"sessionId": "s", "toolCall": {"toolCallId": "other", "title": "write",
                    "rawInput": {}}, "options": [{"optionId": "yes", "kind": "allow_once"}]}})
            else:
                self.cancel_release.set()
                await self.queue.put({"jsonrpc": "2.0", "id": self.prompt_id,
                                      "result": {"stopReason": "cancelled"}})
            return
        if message.get("method") == "session/cancel":
            self.messages.append(message)
            await self.cancel_release.wait()
            return
        await super().write(data)


class InboundCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_turn_cancels_outstanding_permission_callback(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def callback(name, input_data, context):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        transport = PermissionTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=callback),
                                 transport=transport) as client:
            await client.query("first")
            await asyncio.wait_for(started.wait(), 2)
            await transport.queue.put({"jsonrpc": "2.0", "id": transport.prompt_id,
                                       "result": {"stopReason": "end_turn"}})
            result = [message async for message in client.receive_response()][-1]
            self.assertFalse(result.is_error)
            await asyncio.wait_for(cancelled.wait(), 2)
            await client.query("second")
            self.assertEqual([message async for message in client.receive_response()][-1].result, "second")
        replies = [message for message in transport.messages if message.get("id") == 100]
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 100,
                                    "result": {"outcome": {"outcome": "cancelled"}}}])

    async def test_failed_inbound_cancellation_disconnects_completed_session(self):
        started = asyncio.Event()

        async def callback(name, input_data, context):
            started.set()
            await asyncio.Event().wait()

        transport = FailedInboundReplyTransport()
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=callback), transport=transport)
        await client.connect()
        await client.query("first")
        await asyncio.wait_for(started.wait(), 2)
        await transport.queue.put({"jsonrpc": "2.0", "id": transport.prompt_id,
                                   "result": {"stopReason": "end_turn"}})
        self.assertFalse([message async for message in client.receive_response()][-1].is_error)
        self.assertFalse(transport.ready)
        self.assertFalse(client._connected)

    async def test_cancelling_inbound_cleanup_disconnects_completed_session(self):
        started = asyncio.Event()

        async def callback(name, input_data, context):
            started.set()
            await asyncio.Event().wait()

        transport = SlowInboundReplyTransport()
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=callback), transport=transport)
        await client.connect()
        await client.query("first")
        await asyncio.wait_for(started.wait(), 2)
        await transport.queue.put({"jsonrpc": "2.0", "id": transport.prompt_id,
                                   "result": {"stopReason": "end_turn"}})
        async def consume():
            return [message async for message in client.receive_response()]
        task = asyncio.create_task(consume())
        await asyncio.wait_for(transport.reply_started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(transport.ready)
        self.assertFalse(client._connected)

    async def test_permission_arriving_after_interrupt_is_cancelled_without_callback(self):
        calls = []
        async def callback(name, input_data, context):
            calls.append(name)
            return PermissionResultAllow()

        transport = PermissionTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=callback),
                                 transport=transport) as client:
            await client.query("first")
            await client.interrupt()
            result = [message async for message in client.receive_response()][-1]
            self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(calls, [])
        replies = [message for message in transport.messages if message.get("id") == 100]
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 100,
                                    "result": {"outcome": {"outcome": "cancelled"}}}])

    async def test_interrupt_cancels_pending_permission_and_ignores_late_callback(self):
        for late_decision in (PermissionResultAllow(), PermissionResultDeny(interrupt=True)):
            with self.subTest(decision=type(late_decision).__name__):
                started, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

                async def callback(name, input_data, context):
                    started.set()
                    try:
                        await released.wait()
                    except asyncio.CancelledError:
                        await released.wait()
                    finished.set()
                    return late_decision

                transport = PermissionTransport()
                options = KiroAgentOptions(agent_engine="v3", can_use_tool=callback)
                async with KiroSDKClient(options, transport=transport) as client:
                    await client.query("first")
                    await asyncio.wait_for(started.wait(), 2)
                    await client.interrupt()
                    result = [message async for message in client.receive_response()][-1]
                    self.assertEqual(result.stop_reason, "cancelled")
                    await client.query("second")
                    self.assertEqual([message async for message in client.receive_response()][-1].result, "second")
                    released.set()
                    await asyncio.wait_for(finished.wait(), 2)
                    await asyncio.sleep(0)
                replies = [message for message in transport.messages if message.get("id") == 100]
                self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 100,
                                            "result": {"outcome": {"outcome": "cancelled"}}}])
                self.assertEqual(sum(message.get("method") == "session/cancel" for message in transport.messages), 1)

    async def test_agent_request_cancellation_does_not_allow_late_reply(self):
        started, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def callback(name, input_data, context):
            started.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                await released.wait()
            finished.set()
            return PermissionResultAllow()

        transport = PermissionTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=callback),
                                 transport=transport) as client:
            await client.query("first")
            await asyncio.wait_for(started.wait(), 2)
            await transport.queue.put({"jsonrpc": "2.0", "method": "$/cancel_request", "params": {"id": 100}})
            _ = [message async for message in client.receive_response()]
            released.set()
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0)
        replies = [message for message in transport.messages if message.get("id") == 100]
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 100,
                                    "result": {"outcome": {"outcome": "cancelled"}}}])

    async def test_reused_request_id_does_not_revive_old_callback(self):
        started, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0
        async def callback(name, input_data, context):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    await released.wait()
                finished.set()
            return PermissionResultAllow()

        transport = PermissionTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=callback),
                                 transport=transport) as client:
            await client.query("first")
            await asyncio.wait_for(started.wait(), 2)
            await client.interrupt()
            _ = [message async for message in client.receive_response()]
            await client.query("second-permission")
            result = [message async for message in client.receive_response()][-1]
            self.assertEqual(result.stop_reason, "end_turn")
            released.set()
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0)
        replies = [message["result"]["outcome"]["outcome"] for message in transport.messages
                   if message.get("id") == 100]
        self.assertEqual(replies, ["cancelled", "selected"])
        self.assertEqual(calls, 2)

    async def test_id_can_be_reused_after_reply_while_handler_is_interrupting(self):
        async def deny_and_interrupt(name, input_data, context):
            return PermissionResultDeny(interrupt=True)

        transport = ReuseDuringInterruptTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=deny_and_interrupt),
                                 transport=transport) as client:
            await client.query("first")
            result = [message async for message in client.receive_response()][-1]
            self.assertEqual(result.stop_reason, "cancelled")
        replies = [message["result"]["outcome"]["outcome"] for message in transport.messages
                   if message.get("id") == 100]
        self.assertEqual(replies, ["selected", "cancelled"])

    async def test_disconnect_does_not_wait_forever_for_uncooperative_callback(self):
        started, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def callback(name, input_data, context):
            started.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                await released.wait()
            finished.set()
            return PermissionResultAllow()

        transport = PermissionTransport()
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=callback), transport=transport)
        await client.connect()
        await client.query("first")
        await asyncio.wait_for(started.wait(), 2)
        # Disconnect is bounded and reports the still-running callback instead of hiding it.
        with self.assertRaisesRegex(CLIConnectionError, "Request callback cleanup is incomplete"):
            await asyncio.wait_for(client.disconnect(), 5)
        released.set()
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0)
        self.assertFalse(any(message.get("id") == 100 for message in transport.messages))

    async def test_interrupt_dismisses_pending_user_input_and_elicitation(self):
        started_user, started_mcp = asyncio.Event(), asyncio.Event()
        async def user_input(params):
            started_user.set()
            await asyncio.Event().wait()
        async def elicitation(params):
            started_mcp.set()
            await asyncio.Event().wait()

        transport = InteractionTransport()
        options = KiroAgentOptions(agent_engine="v3", on_user_input=user_input, on_mcp_elicitation=elicitation)
        async with KiroSDKClient(options, transport=transport) as client:
            await client.query("first")
            await asyncio.wait_for(asyncio.gather(started_user.wait(), started_mcp.wait()), 2)
            await client.interrupt()
            result = [message async for message in client.receive_response()][-1]
            self.assertEqual(result.stop_reason, "cancelled")
        responses = {message["id"]: message["result"] for message in transport.messages
                     if message.get("id") in (101, 102)}
        self.assertEqual(responses, {101: {"action": "dismissed"}, 102: {"action": "cancel"}})

    async def test_interrupt_does_not_cancel_other_session_request(self):
        started, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def callback(name, input_data, context):
            self.assertEqual(context.session_id, "other")
            started.set()
            await released.wait()
            finished.set()
            return PermissionResultAllow()

        transport = OtherSessionTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=callback),
                                 transport=transport) as client:
            await client.query("first")
            await asyncio.wait_for(started.wait(), 2)
            await client.interrupt()
            _ = [message async for message in client.receive_response()]
            released.set()
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0)
        replies = [message for message in transport.messages if message.get("id") == 103]
        self.assertEqual(replies[0]["result"]["outcome"], {"outcome": "selected", "optionId": "yes"})

    async def test_failed_interrupt_aborts_connection_instead_of_leaving_permission_pending(self):
        started = asyncio.Event()
        async def callback(name, input_data, context):
            started.set()
            await asyncio.Event().wait()

        transport = FailedCancelTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", can_use_tool=callback),
                                 transport=transport) as client:
            await client.query("first")
            await asyncio.wait_for(started.wait(), 2)
            with self.assertRaises(OSError):
                await client.interrupt()
            self.assertFalse(transport.is_ready())
            with self.assertRaisesRegex(Exception, "Interrupt delivery failed"):
                _ = [message async for message in client.receive_response()]


if __name__ == "__main__":
    unittest.main()
