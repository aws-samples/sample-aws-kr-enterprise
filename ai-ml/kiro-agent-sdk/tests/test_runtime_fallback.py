import asyncio
import json
import unittest

from kiro_agent_sdk import (JSONRPCError, KiroAgentOptions, ReportedCreditLimitError, ResultMessage,
                            query_with_fallback)
from test_transport import FakeTransport


class RetryTransport(FakeTransport):
    def __init__(self, *, first="failed", tool=False, reject_model=False):
        super().__init__()
        self.first = first
        self.tool = tool
        self.reject_model = reject_model
        self.turns = 0

    async def write(self, data):
        message = json.loads(data)
        method = message["method"]
        if method == "session/set_config_option" and self.reject_model:
            self.requests.append(message)
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"],
                                  "error": {"code": -32603, "message": "selection failed"}})
            return
        if method != "session/prompt":
            await super().write(data)
            return
        self.requests.append(message)
        self.turns += 1
        if self.first == "rpc-error" and self.turns == 1:
            await self.queue.put({"jsonrpc": "2.0", "id": message["id"],
                                  "error": {"code": -32603, "message": "outcome unknown"}})
            return
        updates = []
        if self.tool and self.turns == 1:
            updates.append({"sessionUpdate": "tool_call", "toolCallId": "write-1",
                            "title": "write", "rawInput": {"path": "file"}})
        text = "failed" if self.turns == 1 else "fallback result"
        updates.append({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})
        status = self.first if self.turns == 1 else "success"
        if status in ("failed", "success", "aborted"):
            updates.append({"sessionUpdate": "session_info_update",
                            "_meta": {"kiro": {"kind": "turn_completion", "status": status}}})
        for update in updates:
            await self.queue.put({"jsonrpc": "2.0", "method": "session/update",
                                  "params": {"sessionId": "remote", "update": update}})
        reason = "cancelled" if status == "cancelled" else "end_turn"
        await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": reason}})


class RuntimeFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_approval_retries_in_same_session_and_preserves_prompt(self):
        transport = RetryTransport(tool=True)
        options = KiroAgentOptions(agent_engine="v3", model="model-one")
        prompt = [{"type": "text", "text": "original"}]
        decisions = []

        async def approve(result, observed_tools):
            decisions.append((result.is_error, observed_tools, result.session_id))
            return True

        results = []
        async for message in query_with_fallback(prompt=prompt, fallback_model="model-two",
                                                  approve_retry=approve, options=options, transport=transport):
            if isinstance(message, ResultMessage):
                results.append(message)
                prompt[0]["text"] = "mutated after first attempt"
        self.assertEqual(decisions, [(True, True, "remote")])
        self.assertEqual([item.is_error for item in results], [True, False])
        self.assertEqual(results[-1].result, "fallback result")
        calls = [item for item in transport.requests if item["method"] == "session/prompt"]
        self.assertEqual(len(calls), 2)
        self.assertEqual([call["params"]["prompt"][0]["text"] for call in calls], ["original", "original"])
        self.assertEqual({call["params"]["sessionId"] for call in calls}, {"remote"})
        self.assertEqual(options.model, "model-one")
        self.assertEqual([item["params"]["value"] for item in transport.requests
                          if item["method"] == "session/set_config_option"], ["model-two"])
        self.assertTrue(transport.closed)

    async def test_retry_requires_approval_and_never_retries_success_or_cancellation(self):
        for status, decision, expected_calls in (("failed", False, 1), ("failed", 1, 1), ("success", True, 0),
                                                  ("cancelled", True, 0), ("aborted", True, 0)):
            transport = RetryTransport(first=status)
            callbacks = []
            async def approve(result, observed_tools):
                callbacks.append((result.is_error, observed_tools))
                return decision
            messages = [item async for item in query_with_fallback(
                prompt="hello", fallback_model="model-two", approve_retry=approve,
                options=KiroAgentOptions(agent_engine="v3"), transport=transport)]
            self.assertEqual(len(callbacks), expected_calls)
            self.assertEqual(transport.turns, 1)
            self.assertEqual(sum(isinstance(item, ResultMessage) for item in messages), 1)

    async def test_unknown_credit_usage_still_blocks_retry(self):
        transport = RetryTransport()
        async def approve(result, observed_tools):
            return True
        with self.assertRaises(ReportedCreditLimitError):
            _ = [item async for item in query_with_fallback(
                prompt="hello", fallback_model="model-two", approve_retry=approve,
                options=KiroAgentOptions(agent_engine="v3", max_reported_credits=1), transport=transport)]
        self.assertEqual(transport.turns, 1)

    async def test_uncertain_transport_and_model_selection_errors_do_not_replay(self):
        async def approve(result, observed_tools):
            return True
        for transport in (RetryTransport(first="rpc-error"), RetryTransport(reject_model=True)):
            with self.subTest(first=transport.first, reject_model=transport.reject_model):
                with self.assertRaises(JSONRPCError):
                    _ = [item async for item in query_with_fallback(
                        prompt="hello", fallback_model="model-two", approve_retry=approve,
                        options=KiroAgentOptions(agent_engine="v3"), transport=transport)]
                self.assertEqual(transport.turns, 1)
                self.assertTrue(transport.closed)

    async def test_closing_after_failed_result_never_starts_a_retry(self):
        transport = RetryTransport()
        callbacks = []
        async def approve(result, observed_tools):
            callbacks.append(True)
            return True
        iterator = query_with_fallback(prompt="hello", fallback_model="model-two", approve_retry=approve,
                                       options=KiroAgentOptions(agent_engine="v3"), transport=transport)
        async for message in iterator:
            if isinstance(message, ResultMessage):
                break
        await iterator.aclose()
        self.assertEqual(callbacks, [])
        self.assertEqual(transport.turns, 1)
        self.assertTrue(transport.closed)

    async def test_cancelling_pending_approval_does_not_replay(self):
        transport = RetryTransport()
        entered = asyncio.Event()
        async def approve(result, observed_tools):
            entered.set()
            await asyncio.Event().wait()
        async def consume():
            return [item async for item in query_with_fallback(
                prompt="hello", fallback_model="model-two", approve_retry=approve,
                options=KiroAgentOptions(agent_engine="v3"), transport=transport)]
        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(transport.turns, 1)
        self.assertTrue(transport.closed)

    async def test_validation_and_same_model_do_not_retry(self):
        async def approve(result, observed_tools):
            raise AssertionError("approval must not run")
        for prompt, model, callback in ((iter(["hello"]), "two", approve), ("hello", "", approve),
                                        ("hello", "two", None)):
            with self.subTest(prompt=prompt, model=model):
                with self.assertRaises((TypeError, ValueError)):
                    _ = [item async for item in query_with_fallback(
                        prompt=prompt, fallback_model=model, approve_retry=callback)]
        transport = RetryTransport()
        _ = [item async for item in query_with_fallback(
            prompt="hello", fallback_model="model-one", approve_retry=approve,
            options=KiroAgentOptions(agent_engine="v3", model="model-one"), transport=transport)]
        self.assertEqual(transport.turns, 1)
