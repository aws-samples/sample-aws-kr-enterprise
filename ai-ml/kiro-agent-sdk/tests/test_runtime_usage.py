"""Regression specifications for reported usage; live execution remains deferred."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ResultMessage
from kiro_agent_sdk.errors import ProtocolError
from kiro_agent_sdk.runtime_usage import (JAVASCRIPT as USAGE, TOKEN_FIELDS, validate_usage_report,
                                         validate_normalized_usage_report)
import test_runtime_policies as fixtures
import test_runtime_model_controls as model_fixtures


def report():
    return {"protocol": 1, "scope": "main-stream-attempts", "attempts": 1, "truncated": False,
            "records": [{"index": 1, "modelId": "model", "status": "completed", "httpAttempts": 1,
                         "invalid": False, "tokens": dict(zip(TOKEN_FIELDS, (100, 20, 40, 0)))}]}


class UsageReportTests(unittest.TestCase):
    def test_zero_and_missing_are_distinct_and_input_is_copied(self):
        original = report()
        result = validate_usage_report(original, enabled=True)
        self.assertTrue(result["complete"])
        self.assertEqual(result["totals"]["cacheWriteInputTokens"], 0)
        original["records"][0]["tokens"]["inputTokens"] = 999
        self.assertEqual(result["records"][0]["tokens"]["inputTokens"], 100)
        original["records"][0]["tokens"]["cacheWriteInputTokens"] = None
        result = validate_usage_report(original, enabled=True)
        self.assertIsNone(result["totals"]["cacheWriteInputTokens"])
        self.assertEqual(result["totals"]["outputTokens"], 20)
        self.assertFalse(result["complete"])

    def test_failure_hidden_http_retries_and_unfinished_streams_are_not_complete_totals(self):
        for changes in ({"status": "failed"}, {"status": "cancelled"}, {"status": "unfinished"},
                        {"httpAttempts": 2}, {"httpAttempts": None}, {"invalid": True}):
            value = report()
            value["records"][0].update(changes)
            result = validate_usage_report(value, enabled=True)
            self.assertFalse(result["complete"])
            self.assertTrue(all(amount is None for amount in result["totals"].values()))
            self.assertEqual(result["reported_totals"]["inputTokens"], 100)

    def test_large_observation_set_marks_incomplete_without_zero_filling(self):
        value = report()
        row = value["records"][0]
        value.update(attempts=129, truncated=True,
                     records=[{**copy.deepcopy(row), "index": number} for number in range(1, 129)])
        result = validate_usage_report(value, enabled=True)
        self.assertFalse(result["complete"])
        self.assertEqual(result["reported_totals"]["inputTokens"], 12800)
        self.assertIsNone(result["totals"]["inputTokens"])
        empty = validate_usage_report({**report(), "attempts": 0, "records": []}, enabled=True)
        self.assertFalse(empty["complete"])
        self.assertTrue(all(amount is None for amount in empty["reported_totals"].values()))

    def test_invalid_values_duplicate_indices_and_unrequested_reports_fail(self):
        bad = []
        for value in (True, -1, 1.5, float("nan"), 9007199254740992):
            item = report()
            item["records"][0]["tokens"]["inputTokens"] = value
            bad.append(item)
        for field, value in (("status", "success"), ("httpAttempts", True), ("modelId", "model\0"),
                             ("index", 2), ("invalid", 0), ("prompt", "must not return prompt")):
            item = report()
            item["records"][0][field] = value
            bad.append(item)
        bad.extend([{**report(), "attempts": 2}, {**report(), "truncated": True},
                    {**report(), "scope": "all-models"}, None])
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ProtocolError):
                validate_usage_report(item, enabled=True)
        with self.assertRaises(ProtocolError):
            validate_usage_report(report(), enabled=False)
        self.assertIsNone(validate_usage_report(None, enabled=False))

    def test_sum_overflow_is_unknown(self):
        value = report()
        value["records"][0]["tokens"]["inputTokens"] = 9007199254740991
        value["records"].append({**copy.deepcopy(value["records"][0]), "index": 2})
        value["attempts"] = 2
        result = validate_usage_report(value, enabled=True)
        self.assertIsNone(result["totals"]["inputTokens"])
        self.assertIsNone(result["reported_totals"]["inputTokens"])
        self.assertFalse(result["complete"])

    def test_retained_totals_are_recomputed_and_encoding_order_is_stable(self):
        value = validate_usage_report(report(), enabled=True)
        checked = validate_normalized_usage_report(value)
        reordered = dict(reversed(list(value.items())))
        self.assertEqual(json.dumps(checked), json.dumps(validate_normalized_usage_report(reordered)))
        for change in ("totals", "reported_totals"):
            bad = copy.deepcopy(value)
            bad[change]["inputTokens"] += 1
            with self.assertRaises(ValueError):
                validate_normalized_usage_report(bad)
        bad = copy.deepcopy(value)
        bad["complete"] = 1
        with self.assertRaises(ValueError):
            validate_normalized_usage_report(bad)
        bad = copy.deepcopy(value)
        bad["totals"]["cacheWriteInputTokens"] = False
        with self.assertRaises(ValueError):
            validate_normalized_usage_report(bad)


class RuntimeUsageClientTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RuntimePolicyTests.asyncSetUp
    options = fixtures.RuntimePolicyTests.options
    prompts = fixtures.RuntimePolicyTests.prompts

    async def test_separate_turns_report_only_kiro_metrics(self):
        async with KiroSDKClient(self.options(runtime_usage_mode="reported")) as client:
            for prompt in ("first", "second"):
                await client.query(prompt)
                values = [m async for m in client.receive_response()]
                result = values[-1]
                self.assertIsInstance(result, ResultMessage)
                self.assertEqual(result.kiro_model_usage["totals"]["inputTokens"], 100)
                self.assertTrue(result.kiro_model_usage["complete"])
        policies = [p["_meta"]["kiro"]["sdkRuntime"] for p in self.prompts()]
        self.assertEqual([p["usageMode"] for p in policies], ["reported", "reported"])
        self.assertNotEqual(policies[0]["requestId"], policies[1]["requestId"])

    async def test_missing_cache_is_unknown_not_zero(self):
        options = self.options(runtime_usage_mode="reported")
        options.env["FAKE_USAGE_MISSING_CACHE"] = "1"
        async with KiroSDKClient(options) as client:
            await client.query("probe")
            results = [m async for m in client.receive_response() if isinstance(m, ResultMessage)]
            self.assertFalse(results[-1].kiro_model_usage["complete"])
            self.assertIsNone(results[-1].kiro_model_usage["totals"]["cacheWriteInputTokens"])

    async def test_result_mirror_keeps_the_report_without_aliasing_returned_data(self):
        async with KiroSDKClient(self.options(runtime_usage_mode="reported")) as client:
            with patch.object(client, "_mirror", AsyncMock()) as mirror:
                await client.query("probe")
                results = [m async for m in client.receive_response() if isinstance(m, ResultMessage)]
            saved = next(call.args[1] for call in mirror.await_args_list if call.args[0] == "result")
            self.assertEqual(saved["kiro_model_usage"], results[-1].kiro_model_usage)
            results[-1].kiro_model_usage["records"][0]["tokens"]["inputTokens"] = 999
            self.assertEqual(saved["kiro_model_usage"]["records"][0]["tokens"]["inputTokens"], 100)

    async def test_bad_receipt_is_not_a_successful_result(self):
        options = self.options(runtime_usage_mode="reported")
        options.env["FAKE_USAGE_BAD_RECEIPT"] = "1"
        async with KiroSDKClient(options) as client:
            with self.assertRaises(ProtocolError):
                await client.query("probe")
                _ = [m async for m in client.receive_response()]

    async def test_cancelled_response_preserves_valid_usage_and_cancellation_reason(self):
        options = self.options(runtime_usage_mode="reported", max_turns=1)
        options.env.update(FAKE_USAGE_CANCEL_RECEIPT="1", FAKE_RUNTIME_MODEL_TURNS="2")
        async with KiroSDKClient(options) as client:
            await client.query("cancelled by runtime")
            results = [m async for m in client.receive_response() if isinstance(m, ResultMessage)]
            result = results[-1]
            self.assertTrue(result.is_error)
            self.assertEqual(result.stop_reason, "cancelled")
            self.assertEqual(result.model_turns, 1)
            self.assertEqual(result.kiro_model_usage["totals"]["inputTokens"], 100)

    async def test_cancellation_without_receipt_stays_unknown_and_stale_receipt_is_rejected(self):
        options = self.options(runtime_usage_mode="reported")
        options.env.update(FAKE_USAGE_CANCEL_RECEIPT="1", FAKE_CANCEL_RECEIPT_MISSING="1")
        async with KiroSDKClient(options) as client:
            await client.query("cancelled without receipt")
            results = [m async for m in client.receive_response() if isinstance(m, ResultMessage)]
            self.assertIsNone(results[-1].kiro_model_usage)
            self.assertIsNone(results[-1].model_turns)
        options.env.pop("FAKE_CANCEL_RECEIPT_MISSING")
        options.env["FAKE_CANCEL_RECEIPT_STALE"] = "1"
        async with KiroSDKClient(options) as client:
            with self.assertRaises(ProtocolError):
                await client.query("stale cancelled receipt")
                _ = [m async for m in client.receive_response()]

    async def test_default_and_mode_validation(self):
        options = self.options()
        async with KiroSDKClient(options) as client:
            await client.query("default")
            results = [m async for m in client.receive_response() if isinstance(m, ResultMessage)]
            self.assertIsNone(results[-1].kiro_model_usage)
            options.runtime_usage_mode = "reported"
            with self.assertRaisesRegex(ValueError, "new client"):
                await client.query("must not send")
            options.runtime_usage_mode = "none"
        for value in ("unknown", None, True):
            with self.assertRaises(ValueError):
                KiroSDKClient(self.options(runtime_usage_mode=value))
        with self.assertRaises(ValueError):
            KiroSDKClient(KiroAgentOptions(runtime_usage_mode="reported"))


class RuntimeUsageJavascriptTests(unittest.TestCase):
    def run_js(self, body):
        model_fixtures.RuntimeModelJavascriptTests.run_javascript(self, USAGE + body)
