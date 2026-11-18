"""Specifications for retained usage and authorized AgentCore archive round trips."""
import copy
import json
import unittest
from unittest.mock import patch

from kiro_agent_sdk import BackgroundTaskManager, BackgroundTaskError, ResultMessage
from kiro_agent_sdk.background_events import encode_structured_result
from kiro_agent_sdk.runtime_usage import validate_usage_report
import test_runtime_usage as usage_fixtures
import test_background_tasks as task_fixtures
import test_agentcore_background_archive as archive_fixtures


class RetainedUsageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = task_fixtures.BackgroundTaskTests.asyncSetUp

    async def run_result(self, event, *, limit=8192, structured=True):
        class Client(task_fixtures.ControlledClient):
            async def receive_response(self):
                yield event
        manager = BackgroundTaskManager(max_retained_result_bytes=limit, retain_structured_output=structured)
        self.addAsyncCleanup(manager.aclose)
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", Client):
            key = await manager.start("work")
            await manager.wait(key)
        return manager, key

    async def test_retention_copies_metrics_and_accounts_for_combined_bytes(self):
        usage = validate_usage_report(usage_fixtures.report(), enabled=True)
        event = ResultMessage("independent-session", 'end_turn', False, result="한글", model_turns=1, structured_output={"ok": True}, kiro_model_usage=usage)
        byte_limit = len("한글".encode()) + len(encode_structured_result({"ok": True}, max_bytes=8192))
        byte_limit += len(encode_structured_result(usage, max_bytes=8192))
        manager, key = await self.run_result(event, limit=byte_limit)
        usage["records"][0]["tokens"]["inputTokens"] = 999
        retained = manager.retained_result(key)
        self.assertEqual(retained.kiro_model_usage["records"][0]["tokens"]["inputTokens"], 100)
        retained.kiro_model_usage["totals"]["inputTokens"] = 999
        self.assertEqual(manager.retained_result(key).kiro_model_usage["totals"]["inputTokens"], 100)
        self.assertNotIn("inputTokens", repr(retained))
        event.kiro_model_usage = validate_usage_report(usage_fixtures.report(), enabled=True)
        small, small_key = await self.run_result(event, limit=byte_limit - 1)
        retained_small = small.retained_result(small_key)
        self.assertEqual(retained_small.result, "한글")
        self.assertIsNone(retained_small.kiro_model_usage)
        self.assertTrue(retained_small.usage_omitted)

    async def test_invalid_derived_report_cannot_be_retained_with_a_successful_text(self):
        usage = validate_usage_report(usage_fixtures.report(), enabled=True)
        usage["totals"]["inputTokens"] += 1
        event = ResultMessage("independent-session", 'end_turn', False, result="otherwise valid", model_turns=1, kiro_model_usage=usage)
        manager, key = await self.run_result(event)
        with self.assertRaises(BackgroundTaskError):
            manager.retained_result(key)

    async def test_later_result_clears_earlier_usage_and_cleanup_still_gates_access(self):
        usage = validate_usage_report(usage_fixtures.report(), enabled=True)
        cleanup_failed = True
        class Client(task_fixtures.ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="first", model_turns=1, kiro_model_usage=usage)
                yield ResultMessage(self.session_id, 'end_turn', False, result="second", model_turns=1)
            async def disconnect(self):
                if cleanup_failed:
                    raise RuntimeError("cleanup")
                self.disconnected = True
        async with BackgroundTaskManager(max_retained_result_bytes=8192) as manager:
            with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", Client):
                key = await manager.start("work")
                self.assertTrue((await manager.wait(key)).cleanup_pending)
                with self.assertRaises(BackgroundTaskError):
                    manager.retained_result(key)
                cleanup_failed = False
                await manager.stop(key)
            retained = manager.retained_result(key)
            self.assertEqual(retained.result, "second")
            self.assertIsNone(retained.kiro_model_usage)


@unittest.skipUnless(archive_fixtures.HAS_STARLETTE, "install the agentcore extra")
class AgentCoreRetainedUsageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = archive_fixtures.AgentCoreBackgroundArchiveTests.asyncSetUp
    asyncTearDown = archive_fixtures.AgentCoreBackgroundArchiveTests.asyncTearDown
    app = archive_fixtures.AgentCoreBackgroundArchiveTests.app
    authorize = archive_fixtures.AgentCoreBackgroundArchiveTests.authorize
    connect = archive_fixtures.AgentCoreBackgroundArchiveTests.connect
    invoke = archive_fixtures.AgentCoreBackgroundArchiveTests.invoke

    async def test_result_archive_replacement_runtime_and_owner_checks_preserve_usage(self):
        from kiro_agent_sdk import SQLiteBackgroundResultStore
        usage = validate_usage_report(usage_fixtures.report(), enabled=True)
        class Client(task_fixtures.ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="answer", model_turns=1, structured_output={"ok": True}, kiro_model_usage=usage)
            async def disconnect(self):
                self.disconnected = True
        store = SQLiteBackgroundResultStore(self.root / "usage.sqlite")
        app = self.app(store)
        manager = await self.connect(app)
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", Client):
            key = await manager.start("work")
            await manager.wait(key)
        code, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": key})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["kiro_model_usage"]["totals"]["inputTokens"], 100)
        self.assertEqual((await self.invoke(app, {"action": "background_task_archive", "task_id": key}))[0], 200)
        stored = await store.load("session-a", key)
        self.assertIn("kiro_model_usage", json.loads(stored)["retained_result"])
        await app.state.kiro_agentcore.close()
        replacement = self.app(store)
        await self.connect(replacement)
        code, body, _ = await self.invoke(replacement, {"action": "background_task_archive_get", "task_id": key})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["retained_result"]["kiro_model_usage"], validate_usage_report(usage_fixtures.report(), enabled=True))
        self.assertEqual((await self.invoke(replacement, {
            "action": "background_task_archive_get", "task_id": key}, identity="other"))[0], 403)
        self.assertEqual(await store.load("session-a", key), stored)

    async def test_archived_usage_cannot_forge_totals_or_escape_combined_retention_limit(self):
        from kiro_agent_sdk.agentcore import _background_retained_data
        usage = validate_usage_report(usage_fixtures.report(), enabled=True)
        base = {"result": "안녕", "is_error": False, "structured_output": {"ok": True}}
        data = {**base, "kiro_model_usage": usage}
        limit = len(base["result"].encode()) + len(encode_structured_result(base["structured_output"], max_bytes=8192))
        limit += len(encode_structured_result(usage, max_bytes=8192))
        self.assertEqual(_background_retained_data(data, limit, True), data)
        self.assertEqual(_background_retained_data(data, limit - 1, True),
                         {**base, "usage_omitted": True})
        bad = copy.deepcopy(data)
        bad["kiro_model_usage"]["reported_totals"]["inputTokens"] = 999
        with self.assertRaises(ValueError):
            _background_retained_data(bad, 8192, True)
        self.assertEqual(_background_retained_data(base, 8192, True), base)
