import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import (AgentDefinition, AgentMessage, BackgroundTaskError, BackgroundTaskManager, HookMatcher,
                            KiroAgentOptions, KiroSDKClient, ProtocolError, ResultMessage, TextBlock)

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class ControlledClient:
    instances = []
    gate = None
    disconnect_gate = None
    event_count = 0
    fail = False
    fail_result = False
    fail_disconnect = False

    def __init__(self, options):
        self.options = options
        self.session_id = "independent-session"
        self.disconnected = False
        self.__class__.instances.append(self)

    async def connect(self):
        pass

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_response(self):
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise ValueError("sensitive text")
        if self.fail_result:
            yield ResultMessage(self.session_id, None, True)
        for item in range(self.event_count):
            yield item

    async def receive_messages(self):
        async for event in self.receive_response():
            yield event

    async def disconnect(self):
        if self.disconnect_gate is not None:
            await self.disconnect_gate.wait()
        if self.fail_disconnect:
            raise RuntimeError("synthetic cleanup failure")
        self.disconnected = True


class BackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ControlledClient.instances = []
        ControlledClient.gate = None
        ControlledClient.disconnect_gate = None
        ControlledClient.event_count = 0
        ControlledClient.fail = False
        ControlledClient.fail_result = False
        ControlledClient.fail_disconnect = False
        self.patcher = patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ControlledClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_events_status_and_capacity(self):
        ControlledClient.event_count = 2
        async with BackgroundTaskManager(max_tasks=1, max_events=2) as manager:
            task_id = await manager.start("hello")
            with self.assertRaises(RuntimeError):
                await manager.start("other")
            status = await manager.wait(task_id)
            self.assertEqual((status.state, status.session_id, status.events_received),
                             ("completed", "independent-session", 2))
            statuses, truncated = manager.statuses(limit=1)
            self.assertEqual(([item.task_id for item in statuses], truncated), ([task_id], False))
            self.assertEqual(manager.active_statuses(), ([], False))
            self.assertEqual([item async for item in manager.events(task_id)], [0, 1])
            manager.forget(task_id)
            with self.assertRaises(KeyError):
                manager.status(task_id)
        with self.assertRaises(RuntimeError):
            await manager.start("closed")

    async def test_overflow_fails_and_disconnects_without_dropping_prior_events(self):
        ControlledClient.event_count = 3
        async with BackgroundTaskManager(max_events=2) as manager:
            task_id = await manager.start("hello")
            status = await manager.wait(task_id)
            self.assertEqual((status.state, status.events_received, status.error_type),
                             ("failed", 2, "BackgroundTaskError"))
            received = []
            with self.assertRaises(BackgroundTaskError):
                async for item in manager.events(task_id):
                    received.append(item)
            self.assertEqual(received, [0, 1])
            self.assertTrue(ControlledClient.instances[0].disconnected)

    async def test_all_task_statuses_are_bounded(self):
        async with BackgroundTaskManager(max_tasks=2) as manager:
            first = await manager.start("one")
            second = await manager.start("two")
            await manager.wait(first)
            await manager.wait(second)
            statuses, truncated = manager.statuses(limit=1)
            self.assertEqual(([item.task_id for item in statuses], truncated), ([first], True))
            for invalid in (0, 1025, True, "1"):
                with self.subTest(limit=invalid), self.assertRaises(ValueError):
                    manager.statuses(limit=invalid)

    async def test_drop_oldest_retains_recent_events_without_a_consumer(self):
        ControlledClient.event_count = 7
        async with BackgroundTaskManager(max_events=2, overflow_policy="drop-oldest") as manager:
            task_id = await manager.start("hello")
            status = await manager.wait(task_id)
            self.assertEqual((status.state, status.events_received, status.events_dropped),
                             ("completed", 7, 5))
            self.assertEqual([item async for item in manager.events(task_id)], [5, 6])
            self.assertTrue(ControlledClient.instances[0].disconnected)

    async def test_drop_oldest_during_slow_consumption_reports_loss(self):
        release = asyncio.Event()

        class StagedClient(ControlledClient):
            async def receive_response(self):
                yield 0
                await release.wait()
                for item in range(1, 6):
                    yield item

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", StagedClient):
            async with BackgroundTaskManager(max_events=2, overflow_policy="drop-oldest") as manager:
                task_id = await manager.start("hello")
                reader = manager.events(task_id)
                self.assertEqual(await reader.__anext__(), 0)
                release.set()
                status = await manager.wait(task_id)
                self.assertEqual((status.state, status.events_received, status.events_dropped),
                                 ("completed", 6, 3))
                self.assertEqual([item async for item in reader], [4, 5])

    async def test_drop_oldest_still_reports_failure_if_error_event_is_evicted(self):
        ControlledClient.fail_result = True
        ControlledClient.event_count = 2
        async with BackgroundTaskManager(max_events=1, overflow_policy="drop-oldest") as manager:
            task_id = await manager.start("hello")
            status = await manager.wait(task_id)
            self.assertEqual((status.state, status.events_received, status.events_dropped),
                             ("failed", 3, 2))
            received = []
            with self.assertRaises(BackgroundTaskError):
                async for item in manager.events(task_id):
                    received.append(item)
            self.assertEqual(received, [1])

    async def test_optional_retained_result_survives_event_eviction_and_consumption(self):
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="private answer")
                yield "later event"

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            async with BackgroundTaskManager(max_events=1, overflow_policy="drop-oldest",
                                             max_retained_result_bytes=64) as manager:
                task_id = await manager.start("hello")
                self.assertEqual((await manager.wait(task_id)).state, "completed")
                retained = manager.retained_result(task_id)
                self.assertEqual((retained.task_id, retained.result, retained.is_error),
                                 (task_id, "private answer", False))
                self.assertNotIn("private answer", repr(retained))
                self.assertEqual([item async for item in manager.events(task_id)], ["later event"])
                self.assertEqual(manager.retained_result(task_id), retained)
                manager.forget(task_id)
                with self.assertRaises(KeyError):
                    manager.retained_result(task_id)

    async def test_retained_result_is_bounded_and_does_not_hide_task_failure(self):
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, None, True, result="secret")

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            async with BackgroundTaskManager(max_retained_result_bytes=3) as manager:
                task_id = await manager.start("hello")
                self.assertEqual((await manager.wait(task_id)).state, "failed")
                with self.assertRaises(BackgroundTaskError) as error:
                    manager.retained_result(task_id)
                self.assertNotIn("secret", str(error.exception))

    async def test_optional_structured_output_is_copied_and_requires_opt_in(self):
        output = {"items": [{"name": "private"}]}
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="text", structured_output=output)

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            async with BackgroundTaskManager(max_retained_result_bytes=256,
                                             retain_structured_output=True) as manager:
                task_id = await manager.start("hello")
                await manager.wait(task_id)
                output["items"][0]["name"] = "changed"
                retained = manager.retained_result(task_id)
                self.assertEqual(retained.structured_output, {"items": [{"name": "private"}]})
                self.assertNotIn("private", repr(retained))
                retained.structured_output["items"][0]["name"] = "caller mutation"
                self.assertEqual(manager.retained_result(task_id).structured_output, {"items": [{"name": "private"}]})
            async with BackgroundTaskManager(max_retained_result_bytes=256) as manager:
                task_id = await manager.start("hello")
                await manager.wait(task_id)
                self.assertIsNone(manager.retained_result(task_id).structured_output)
        for changes in ({"retain_structured_output": True},
                        {"max_retained_result_bytes": 32, "retain_structured_output": "true"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                BackgroundTaskManager(**changes)

    async def test_invalid_or_oversized_structured_output_is_not_partially_retained(self):
        class ResultClient(ControlledClient):
            output = None
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="secret", structured_output=self.output)

        cycle = []
        cycle.append(cycle)
        for value in (cycle, {"bad": float("nan")}, {"bad": object()}, {"large": "x" * 40}):
            ResultClient.output = value
            with self.subTest(kind=type(value).__name__), patch(
                    "kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
                async with BackgroundTaskManager(max_retained_result_bytes=32,
                                                 retain_structured_output=True) as manager:
                    task_id = await manager.start("hello")
                    self.assertEqual((await manager.wait(task_id)).state, "completed")
                    with self.assertRaises(BackgroundTaskError) as error:
                        manager.retained_result(task_id)
                    self.assertNotIn("secret", str(error.exception))

    async def test_later_result_not_queued_does_not_leave_a_stale_retained_result(self):
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="first")
                yield ResultMessage(self.session_id, 'end_turn', False, result="later")

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            async with BackgroundTaskManager(max_events=1, max_retained_result_bytes=64) as manager:
                task_id = await manager.start("hello")
                self.assertEqual((await manager.wait(task_id)).state, "failed")
                self.assertIsNone(manager.retained_result(task_id))

    async def test_retention_is_explicit_and_requires_a_finished_task(self):
        ControlledClient.gate = asyncio.Event()
        async with BackgroundTaskManager(max_retained_result_bytes=64) as manager:
            task_id = await manager.start("hello")
            with self.assertRaisesRegex(RuntimeError, "not finished"):
                manager.retained_result(task_id)
            ControlledClient.gate.set()
            await manager.wait(task_id)
            self.assertIsNone(manager.retained_result(task_id))
        async with BackgroundTaskManager() as manager:
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                manager.retained_result(task_id)
        for size in (-1, 1024 * 1024 + 1, True, 1.5, None):
            with self.subTest(size=size), self.assertRaises(ValueError):
                BackgroundTaskManager(max_retained_result_bytes=size)

    async def test_event_history_is_bounded_nonconsuming_and_reports_cursor_gaps(self):
        first = AgentMessage([TextBlock("first")])
        second = AgentMessage([TextBlock("second")])
        third = ResultMessage("independent-session", 'end_turn', False, result="third")

        class EventClient(ControlledClient):
            async def receive_response(self):
                yield first
                yield second
                yield third

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            async with BackgroundTaskManager(max_history_events=2, max_history_bytes=8192) as manager:
                task_id = await manager.start("hello")
                await manager.wait(task_id)
                second.content[0].text = "changed after capture"
                page = manager.event_history(task_id, after=0, limit=1)
                self.assertEqual((page.next_cursor, page.latest_sequence, page.history_dropped, page.gap, page.has_more),
                                 (2, 3, 1, True, True))
                self.assertEqual(page.events[0]["message"]["content"][0]["text"], "second")
                self.assertNotIn("second", repr(page))
                page.events[0]["message"]["content"][0]["text"] = "caller mutation"
                self.assertEqual(manager.event_history(task_id, after=0).events[0]["message"]["content"][0]["text"],
                                 "second")
                last = manager.event_history(task_id, after=2)
                self.assertEqual((last.next_cursor, last.gap, last.has_more), (3, False, False))
                self.assertEqual(last.events[0]["type"], "ResultMessage")
                self.assertEqual(len([item async for item in manager.events(task_id)]), 3)
                self.assertEqual(len(manager.event_history(task_id).events), 2)

    async def test_event_history_skips_oversized_and_unsupported_events_without_failing_task(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("safe")])
                yield AgentMessage([TextBlock("x" * 2048)])
                yield object()
                yield ResultMessage("independent-session", 'end_turn', False, result="done")

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            async with BackgroundTaskManager(max_history_events=8, max_history_bytes=1024) as manager:
                task_id = await manager.start("hello")
                self.assertEqual((await manager.wait(task_id)).state, "completed")
                page = manager.event_history(task_id)
                self.assertEqual([item["sequence"] for item in page.events], [1, 4])
                self.assertEqual((page.next_cursor, page.latest_sequence, page.history_dropped, page.gap),
                                 (4, 4, 2, True))
                self.assertFalse(page.has_more)

    async def test_event_history_requires_explicit_limits_and_valid_cursors(self):
        for changes in ({"max_history_events": 1}, {"max_history_bytes": 1},
                        {"max_history_events": True, "max_history_bytes": 1},
                        {"max_history_events": 1025, "max_history_bytes": 1},
                        {"max_history_events": 1, "max_history_bytes": 524289}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                BackgroundTaskManager(**changes)
        async with BackgroundTaskManager(max_history_events=2, max_history_bytes=1024) as manager:
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            for after, limit in ((-1, 1), (1, 1), (False, 1), (0, 0), (0, 65), (0, True)):
                with self.subTest(after=after, limit=limit), self.assertRaises(ValueError):
                    manager.event_history(task_id, after=after, limit=limit)
        async with BackgroundTaskManager() as manager:
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                manager.event_history(task_id)

    async def test_cancelled_waiting_consumer_does_not_lose_later_events(self):
        ControlledClient.gate = asyncio.Event()
        ControlledClient.event_count = 2
        async with BackgroundTaskManager(max_events=2) as manager:
            task_id = await manager.start("hello")
            reader = manager.events(task_id)
            pending = asyncio.create_task(reader.__anext__())
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            ControlledClient.gate.set()
            self.assertEqual((await manager.wait(task_id)).state, "completed")
            self.assertEqual([item async for item in manager.events(task_id)], [0, 1])

    async def test_cancel_at_event_delivery_does_not_silently_discard_it(self):
        ControlledClient.gate = asyncio.Event()
        ControlledClient.event_count = 1
        async with BackgroundTaskManager(max_events=1) as manager:
            task_id = await manager.start("hello")
            reader = manager.events(task_id)
            pending = asyncio.create_task(reader.__anext__())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            ControlledClient.gate.set()
            await asyncio.sleep(0)
            asyncio.get_running_loop().call_soon(pending.cancel)
            try:
                first = await pending
            except asyncio.CancelledError:
                received = [item async for item in manager.events(task_id)]
            else:
                received = [first] + [item async for item in reader]
            self.assertEqual(received, [0])
            self.assertEqual((await manager.wait(task_id)).events_dropped, 0)

    async def test_overflow_policy_validation(self):
        for policy in ("discard", "", None, True, []):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                BackgroundTaskManager(overflow_policy=policy)

    async def test_active_reader_can_keep_up_with_buffered_source(self):
        ControlledClient.event_count = 20
        async with BackgroundTaskManager(max_events=1) as manager:
            task_id = await manager.start("hello")
            self.assertEqual([item async for item in manager.events(task_id)], list(range(20)))
            self.assertEqual((await manager.wait(task_id)).state, "completed")

    async def test_stalled_reader_times_out_and_disconnects(self):
        ControlledClient.event_count = 3
        async with BackgroundTaskManager(max_events=1, event_buffer_timeout=0.01) as manager:
            task_id = await manager.start("hello")
            reader = manager.events(task_id)
            self.assertEqual(await reader.__anext__(), 0)
            status = await manager.wait(task_id)
            self.assertEqual((status.state, status.error_type), ("failed", "BackgroundTaskError"))
            await reader.aclose()
            self.assertTrue(ControlledClient.instances[0].disconnected)

    async def test_cancel_before_start_and_while_running(self):
        ControlledClient.gate = asyncio.Event()
        async with BackgroundTaskManager() as manager:
            task_id = await manager.start("immediate")
            self.assertEqual((await manager.stop(task_id)).state, "cancelled")
            task_id = await manager.start("running")
            await asyncio.sleep(0)
            self.assertEqual(manager.status(task_id).state, "running")
            with self.assertRaises(RuntimeError):
                manager.forget(task_id)
            self.assertEqual((await manager.stop(task_id)).state, "cancelled")
            self.assertTrue(ControlledClient.instances[-1].disconnected)

    async def test_client_stops_only_tasks_in_explicitly_attached_manager(self):
        ControlledClient.gate = asyncio.Event()
        client = KiroSDKClient()
        async with BackgroundTaskManager() as manager, BackgroundTaskManager() as other:
            task_id = await manager.start("running")
            other_id = await other.start("other")
            await asyncio.sleep(0)
            self.assertTrue(manager.owns(task_id))
            self.assertFalse(manager.owns(other_id))
            with self.assertRaises(ProtocolError):
                await client.stop_task(task_id)
            client.attach_background_tasks(manager)
            with self.assertRaises(ProtocolError):
                await client.stop_task(other_id)
            self.assertIsNone(await client.stop_task(task_id))
            self.assertEqual(manager.status(task_id).state, "cancelled")
            self.assertEqual(other.status(other_id).state, "running")
            client.attach_background_tasks(None)
            with self.assertRaises(ProtocolError):
                await client.stop_task(task_id)
            manager.forget(task_id)
            self.assertFalse(manager.owns(task_id))
            with self.assertRaises(TypeError):
                client.attach_background_tasks(object())

    async def test_attached_manager_remains_caller_owned_and_ambiguous_ids_are_rejected(self):
        ControlledClient.gate = asyncio.Event()
        client = KiroSDKClient()
        async with BackgroundTaskManager() as manager:
            task_id = await manager.start("running")
            await asyncio.sleep(0)
            client.attach_background_tasks(manager)
            client._owned_workflows.add(task_id)
            with self.assertRaisesRegex(ProtocolError, "ambiguous"):
                await client.stop_task(task_id)
            client._owned_workflows.clear()
            await client.disconnect()
            self.assertEqual(manager.status(task_id).state, "running")
            await client.stop_task(task_id)
            self.assertEqual(manager.status(task_id).state, "cancelled")

    async def test_client_reports_background_cleanup_failure(self):
        ControlledClient.gate = asyncio.Event()
        ControlledClient.fail_disconnect = True
        client = KiroSDKClient()
        async with BackgroundTaskManager() as manager:
            task_id = await manager.start("running")
            await asyncio.sleep(0)
            client.attach_background_tasks(manager)
            with self.assertRaisesRegex(ProtocolError, "failed"):
                await client.stop_task(task_id)
            self.assertEqual(manager.status(task_id).state, "failed")
            self.assertTrue(manager.status(task_id).cleanup_pending)
            ControlledClient.fail_disconnect = False

    async def test_cancel_timeout_does_not_repeat_cancellation_during_cleanup(self):
        ControlledClient.gate = asyncio.Event()
        ControlledClient.disconnect_gate = asyncio.Event()
        manager = BackgroundTaskManager(shutdown_timeout=0.01)
        task_id = await manager.start("running")
        await asyncio.sleep(0)
        with self.assertRaises(TimeoutError):
            await manager.stop(task_id)
        self.assertEqual(manager.status(task_id).state, "cancelling")
        with self.assertRaises(TimeoutError):
            await manager.aclose()
        ControlledClient.disconnect_gate.set()
        await manager.aclose()
        self.assertEqual(manager.status(task_id).state, "cancelled")
        self.assertTrue(ControlledClient.instances[-1].disconnected)

    async def test_stop_during_normal_completion_waits_for_cleanup(self):
        ControlledClient.disconnect_gate = asyncio.Event()
        manager = BackgroundTaskManager(shutdown_timeout=.01)
        try:
            task_id = await manager.start("already answered")
            await asyncio.sleep(0)
            with self.assertRaises(TimeoutError):
                await manager.stop(task_id)
            self.assertFalse(ControlledClient.instances[-1].disconnected)
            self.assertFalse(manager._get(task_id).done.is_set())
            self.assertTrue(manager.status(task_id).cleanup_pending)
            with self.assertRaises(RuntimeError):
                manager.forget(task_id)
        finally:
            ControlledClient.disconnect_gate.set()
            await manager.aclose()
        self.assertTrue(ControlledClient.instances[-1].disconnected)
        self.assertEqual(manager.status(task_id).state, "completed")
        self.assertFalse(manager.status(task_id).cleanup_pending)

    async def test_failed_cleanup_remains_owned_and_is_retried_without_restarting(self):
        ControlledClient.fail_disconnect = True
        manager = BackgroundTaskManager()
        try:
            task_id = await manager.start("do once")
            status = await manager.wait(task_id)
            self.assertEqual(status.state, "failed")
            self.assertTrue(status.cleanup_pending)
            self.assertEqual(manager.active_statuses()[0][0].task_id, task_id)
            with self.assertRaisesRegex(BackgroundTaskError, "cleanup is incomplete"):
                await manager.start("must not run")
            with self.assertRaises(RuntimeError):
                manager.forget(task_id)
            with self.assertRaises(BackgroundTaskError):
                await manager.aclose()
        finally:
            ControlledClient.fail_disconnect = False
            await manager.aclose()
        self.assertEqual(len(ControlledClient.instances), 1)
        self.assertTrue(ControlledClient.instances[0].disconnected)
        self.assertFalse(manager.status(task_id).cleanup_pending)
        manager.forget(task_id)

    async def test_stop_retries_cleanup_and_preserves_failed_job_status(self):
        ControlledClient.fail_disconnect = True
        manager = BackgroundTaskManager(max_retained_result_bytes=256)
        try:
            task_id = await manager.start("do once")
            await manager.wait(task_id)
            with self.assertRaisesRegex(BackgroundTaskError, "cleanup is incomplete"):
                manager.retained_result(task_id)
            self.assertTrue((await manager.stop(task_id)).cleanup_pending)
            ControlledClient.fail_disconnect = False
            status = await manager.stop(task_id)
            self.assertEqual(status.state, "failed")
            self.assertFalse(status.cleanup_pending)
            self.assertEqual(len(ControlledClient.instances), 1)
            self.assertTrue(ControlledClient.instances[0].disconnected)
            # Successful cleanup does not rerun work or erase the earlier failure.
            self.assertIsNone(manager.retained_result(task_id))
            next_id = await manager.start("next job")
            self.assertEqual((await manager.wait(next_id)).state, "completed")
        finally:
            ControlledClient.fail_disconnect = False
            await manager.aclose()

    async def test_cancelled_stop_waiter_does_not_cancel_cleanup(self):
        ControlledClient.disconnect_gate = asyncio.Event()
        manager = BackgroundTaskManager(shutdown_timeout=5)
        task_id = await manager.start("answered")
        await asyncio.sleep(0)
        stopping = asyncio.create_task(manager.stop(task_id))
        try:
            await asyncio.sleep(0)
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            self.assertTrue(manager.status(task_id).cleanup_pending)
            self.assertFalse(manager._get(task_id).done.is_set())
            # Even direct repeated worker cancellation cannot cancel its owned cleanup.
            worker = manager._get(task_id).worker
            worker.cancel()
            await asyncio.sleep(0)
            worker.cancel()
            await asyncio.sleep(0)
            self.assertFalse(worker.done())
        finally:
            ControlledClient.disconnect_gate.set()
            await manager.aclose()
        self.assertTrue(ControlledClient.instances[0].disconnected)
        self.assertFalse(manager.status(task_id).cleanup_pending)

    async def test_task_guards_stop_authorizing_when_cleanup_starts(self):
        ControlledClient.disconnect_gate = asyncio.Event()
        manager = BackgroundTaskManager()
        try:
            task_id = await manager._start_guarded("answered", KiroAgentOptions(),
                                                   lambda: asyncio.sleep(0, result=True))
            async def cleaning():
                while not manager._get(task_id).cleanup_started:
                    await asyncio.sleep(0)
            await asyncio.wait_for(cleaning(), 3)
            client = ControlledClient.instances[0]
            self.assertFalse(await client._prompt_send_guard())
            self.assertFalse(await client._interaction_guard())
            self.assertFalse(client._client_auth_factory_guard())
        finally:
            ControlledClient.disconnect_gate.set()
            await manager.aclose()

    async def test_client_owned_authentication_is_denied_during_background_cleanup(self):
        from unittest.mock import AsyncMock
        from test_kiro_client_auth import SECRET, token
        self.patcher.stop()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def provider():
            nonlocal calls
            calls += 1
            return token()
        async def idle_authorizer(session_id):
            return True
        original = KiroSDKClient.disconnect
        async def paused_disconnect(client):
            entered.set()
            await release.wait()
            await original(client)
        with tempfile.TemporaryDirectory() as root, patch.object(KiroSDKClient, "disconnect", paused_disconnect):
            manager = BackgroundTaskManager()
            task_id = await manager.start("hello", options=KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[],
                kiro_auth_method="client", kiro_access_token_provider=provider,
                kiro_client_auth_idle_mode="authorized", kiro_idle_token_authorizer=idle_authorizer,
                env={"KIRO_HOME": root, "FAKE_CLIENT_AUTH_INIT": "1", "FAKE_EXPECT_TOKEN": SECRET}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                client = manager._get(task_id).client
                before = calls
                self.assertGreater(before, 0)
                record = {"id": "late-auth", "turn": None, "cancelled": False, "responded": False}
                with patch.object(client, "_send", new_callable=AsyncMock) as send:
                    await client._handle_client_auth_request(
                        {"params": {}, "method": "_kiro/auth/getAccessToken"}, record)
                self.assertEqual(calls, before)
                self.assertIn("error", send.await_args.args[0])
            finally:
                release.set()
                await manager.aclose()
            self.assertIsNone(client._process)

    async def test_failure_does_not_expose_error_message_in_status(self):
        ControlledClient.fail = True
        async with BackgroundTaskManager() as manager:
            task_id = await manager.start("secret prompt")
            status = await manager.wait(task_id)
            self.assertEqual((status.state, status.error_type), ("failed", "ValueError"))
            self.assertNotIn("sensitive", repr(status))
            with self.assertRaises(BackgroundTaskError) as caught:
                _ = [item async for item in manager.events(task_id)]
            self.assertIsInstance(caught.exception.__cause__, ValueError)

    async def test_error_result_is_delivered_and_marks_the_job_failed(self):
        ControlledClient.fail_result = True
        async with BackgroundTaskManager() as manager:
            task_id = await manager.start("hello")
            received = []
            with self.assertRaises(BackgroundTaskError):
                async for item in manager.events(task_id):
                    received.append(item)
            self.assertEqual(len(received), 1)
            self.assertTrue(received[0].is_error)
            self.assertEqual((await manager.wait(task_id)).state, "failed")

    async def test_separate_sessions_and_options_validation(self):
        async with BackgroundTaskManager() as manager:
            with self.assertRaises(ValueError):
                await manager.start("hello", options=KiroAgentOptions(resume="existing"))
            with self.assertRaises(TypeError):
                await manager.start(object())
            prompt = [{"type": "text", "text": "original"}]
            task_id = await manager.start(prompt)
            prompt[0]["text"] = "changed"
            await manager.wait(task_id)
            self.assertEqual(ControlledClient.instances[0].prompt[0]["text"], "original")

    async def test_fake_kiro_session(self):
        self.patcher.stop()
        with tempfile.TemporaryDirectory() as root:
            async with BackgroundTaskManager() as manager:
                task_id = await manager.start("hello", options=KiroAgentOptions(
                    cwd=root, cli_path=FAKE, agent_engine="v3"))
                messages = [item async for item in manager.events(task_id)]
                self.assertTrue(any(isinstance(item, ResultMessage) for item in messages))
                self.assertEqual((await manager.wait(task_id)).state, "completed")

    async def test_background_agent_initial_prompt_and_requested_prompt_both_run(self):
        self.patcher.stop()
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3", agent="selected",
                agents={"selected": AgentDefinition("Test", "Test", initial_prompt="initial")},
                env={"KIRO_HOME": str(Path(root) / "kiro")})
            async with BackgroundTaskManager(max_retained_result_bytes=256) as manager:
                task_id = await manager.start("requested", options=options)
                messages = [item async for item in manager.events(task_id)]
                self.assertEqual([item.result for item in messages if isinstance(item, ResultMessage)],
                                 ["initial", "requested"])
                self.assertEqual((await manager.wait(task_id)).state, "completed")
                self.assertEqual(manager.retained_result(task_id).result, "requested")

    async def test_failed_requested_prompt_does_not_return_initial_output(self):
        self.patcher.stop()
        async def block_requested(event, tool_id, context):
            return {"block": True} if event["prompt"] == "requested" else {}
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(cwd=root, cli_path=FAKE, agent_engine="v3", agent="selected",
                agents={"selected": AgentDefinition("Test", "Test", initial_prompt="initial")},
                hooks={"promptSubmit": [HookMatcher(hooks=[block_requested])]},
                env={"KIRO_HOME": str(Path(root) / "kiro")})
            async with BackgroundTaskManager(max_retained_result_bytes=256) as manager:
                task_id = await manager.start("requested", options=options)
                self.assertEqual((await manager.wait(task_id)).state, "failed")
                self.assertIsNone(manager.retained_result(task_id))
