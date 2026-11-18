import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (AgentMessage, KiroAgentOptions,
                            ReportedCreditLimitError, ResultError, ResultMessage, SQLiteCreditLedger,
                            query_with_structured_output_retry)

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class StructuredOutputRetryTests(unittest.IsolatedAsyncioTestCase):
    def options(self, **kwargs):
        return KiroAgentOptions(cli_path=FAKE, agent_engine="v3",
            output_format={"type": "json_schema", "schema": {
                "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}},
            **kwargs)

    async def test_approved_retry_resumes_original_session_and_preserves_options(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = self.options(cwd=root, env={"FAKE_LOG_REQUESTS": str(log)})
            decisions = []
            async def approve(error, tools, attempt):
                decisions.append((error.kind, error.result, error.session_id, tools, attempt))
                error.session_id = "different-session"
                options.output_format["schema"]["required"] = ["changed"]
                return True
            messages = [m async for m in query_with_structured_output_retry(
                prompt="not-json", options=options, approve_retry=approve, retry_prompt='{"ok": true}')]
            self.assertEqual(decisions, [("invalid_structured_output", "not-json", "test-session", False, 1)])
            self.assertTrue(any(isinstance(m, AgentMessage) for m in messages))
            result = next(m for m in messages if isinstance(m, ResultMessage))
            self.assertEqual(result.structured_output, {"ok": True})
            self.assertIsNone(options.resume)
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([r["params"]["sessionId"] for r in requests if r.get("method") == "session/load"],
                             ["test-session"])
            prompts = [r["params"]["prompt"][0]["text"] for r in requests if r.get("method") == "session/prompt"]
            self.assertEqual(prompts, ["not-json", '{"ok": true}'])

    async def test_declined_retry_preserves_original_validation_error(self):
        decisions = []
        async def decline(error, tools, attempt):
            decisions.append(attempt)
            return False
        with self.assertRaises(ResultError) as caught:
            _ = [m async for m in query_with_structured_output_retry(
                prompt="not-json", options=self.options(), approve_retry=decline)]
        self.assertEqual(caught.exception.kind, "invalid_structured_output")
        self.assertEqual(decisions, [1])

    async def test_callback_is_told_about_streamed_tool_calls(self):
        seen = []
        async def decline(error, tools, attempt):
            seen.append(tools)
            return False
        with self.assertRaises(ResultError):
            _ = [m async for m in query_with_structured_output_retry(
                prompt="nested", options=self.options(), approve_retry=decline)]
        self.assertEqual(seen, [True])

    async def test_retry_respects_shared_reported_credit_limit(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = SQLiteCreditLedger(Path(root) / "credits.sqlite")
            log = Path(root) / "requests.jsonl"
            options = self.options(cwd=root, env={"FAKE_LOG_REQUESTS": str(log)}, max_reported_credits=0.1,
                                   reported_credit_ledger=ledger, reported_credit_scope="retry-test")
            async def approve(error, tools, attempt):
                return True
            with self.assertRaises(ReportedCreditLimitError):
                _ = [m async for m in query_with_structured_output_retry(
                    prompt="not-json", options=options, approve_retry=approve, retry_prompt='{"ok": true}')]
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(sum(item.get("method") == "session/prompt" for item in requests), 1)

    async def test_retries_are_bounded_and_rechecked_after_each_invalid_result(self):
        decisions = []
        async def approve(error, tools, attempt):
            decisions.append(attempt)
            return True
        with self.assertRaises(ResultError):
            _ = [m async for m in query_with_structured_output_retry(
                prompt="not-json", options=self.options(), approve_retry=approve,
                retry_prompt="still-invalid", max_retries=2)]
        self.assertEqual(decisions, [1, 2])

    async def test_invalid_options_fail_before_starting_a_session(self):
        async def approve(error, tools, attempt):
            return True
        for kwargs in (
            {"options": self.options(max_reported_credits=1)},
            {"options": self.options(model="model-one", fallback_model="model-two",
                                     fallback_model_mode="startup")},
            {"options": KiroAgentOptions()},
            {"options": self.options(), "max_retries": 4},
            {"options": self.options(), "max_retries": True},
            {"options": self.options(), "retry_prompt": " "},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    _ = [m async for m in query_with_structured_output_retry(
                        prompt="hello", approve_retry=approve, **kwargs)]

