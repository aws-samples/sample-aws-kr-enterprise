import json
import unittest

from kiro_agent_sdk import JSONRPCError, KiroAgentOptions, KiroSDKClient, ResultMessage
from test_transport import FakeTransport


class StatusTransport(FakeTransport):
    def __init__(self, statuses, stop_reason="end_turn"):
        super().__init__()
        self.statuses = statuses
        self.stop_reason = stop_reason

    async def write(self, data):
        message = json.loads(data)
        if message.get("method") == "session/prompt":
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "remote", "update": {"sessionUpdate": "agent_message_chunk",
                                                    "content": {"type": "text", "text": "not json"}}}})
            for metadata in self.statuses:
                await self.queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "remote", "update": {"sessionUpdate": "session_info_update",
                                                        "_meta": {"kiro": {"kind": "turn_completion", **metadata}}}}})
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"],
                                  "result": {"stopReason": self.stop_reason}})
        else:
            await super().write(data)


class ResultStatusTests(unittest.IsolatedAsyncioTestCase):
    async def result(self, statuses, reason="end_turn", output_format=None):
        options = KiroAgentOptions(agent_engine="v3", output_format=output_format)
        async with KiroSDKClient(options, transport=StatusTransport(statuses, reason)) as client:
            await client.query("start")
            messages = [message async for message in client.receive_response()]
        return next(message for message in messages if isinstance(message, ResultMessage))

    async def test_failed_status_is_reported_as_error(self):
        result = await self.result([{"status": "failed"}], output_format={
            "type": "json_schema", "schema": {"type": "object"}})
        self.assertTrue(result.is_error)
        self.assertEqual(result.result, "not json")
        self.assertIsNone(result.structured_output)

    async def test_cancellation_is_error_even_without_usage_status(self):
        result = await self.result([], reason="cancelled")
        self.assertTrue(result.is_error)
        self.assertEqual(result.stop_reason, "cancelled")

    async def test_success_after_retry_and_subagent_failure_are_not_root_failure(self):
        result = await self.result([{"status": "failed"}, {"status": "success"},
                                    {"status": "failed", "agentSubtaskId": "child"}])
        self.assertFalse(result.is_error)
        self.assertEqual(result.kiro_usage["status"], "success")

    async def test_refusal_preserves_stop_reason_without_json_validation(self):
        result = await self.result([{"status": "failed"}], reason="refusal", output_format={
            "type": "json_schema", "schema": {"type": "object"}})
        self.assertFalse(result.is_error)
        self.assertEqual(result.stop_reason, "refusal")
        self.assertIsNone(result.structured_output)

    async def test_cancel_rpc_error_becomes_result_and_session_can_continue(self):
        class CancelTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.prompt_id = None
                self.count = 0
            async def write(self, data):
                message = json.loads(data)
                if message.get("method") == "session/prompt":
                    self.count += 1
                    if self.count == 1:
                        self.requests.append(message)
                        self.prompt_id = message["id"]
                        return
                if message.get("method") == "session/cancel":
                    self.requests.append(message)
                    await self.queue.put({"jsonrpc": "2.0", "id": self.prompt_id, "error": {
                        "code": -32603, "message": "Internal error", "data": {"details": "This operation was aborted"}}})
                else:
                    await super().write(data)
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=CancelTransport()) as client:
            await client.query("long turn")
            await client.interrupt()
            first = [m async for m in client.receive_response()]
            self.assertEqual((first[-1].is_error, first[-1].stop_reason), (True, "cancelled"))
            await client.query("next")
            second = [m async for m in client.receive_response()]
            self.assertEqual(second[-1].result, "next")

    async def test_unrelated_rpc_error_after_interrupt_is_not_hidden(self):
        class ErrorTransport(FakeTransport):
            async def write(self, data):
                message = json.loads(data)
                if message.get("method") == "session/prompt":
                    self.requests.append(message)
                    self.prompt_id = message["id"]
                elif message.get("method") == "session/cancel":
                    await self.queue.put({"jsonrpc": "2.0", "id": self.prompt_id, "error": {
                        "code": -32603, "message": "Internal error", "data": {"details": "Database failure"}}})
                else:
                    await super().write(data)
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=ErrorTransport()) as client:
            await client.query("long turn")
            await client.interrupt()
            with self.assertRaises(JSONRPCError) as error:
                _ = [m async for m in client.receive_response()]
        self.assertEqual(error.exception.data["details"], "Database failure")

    async def test_cancel_rpc_error_without_interrupt_is_not_hidden(self):
        class ErrorTransport(FakeTransport):
            async def write(self, data):
                message = json.loads(data)
                if message.get("method") == "session/prompt":
                    self.requests.append(message)
                    self.prompt_id = message["id"]
                else:
                    await super().write(data)
        transport = ErrorTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            await client.query("long turn")
            await transport.queue.put({"jsonrpc": "2.0", "id": transport.prompt_id,
                                       "error": {"code": -32800, "message": "Request cancelled"}})
            with self.assertRaises(JSONRPCError):
                _ = [m async for m in client.receive_response()]


if __name__ == "__main__":
    unittest.main()
