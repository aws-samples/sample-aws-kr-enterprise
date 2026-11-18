import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request
from test_background_tasks import ControlledClient

if HAS_STARLETTE:
    from kiro_agent_sdk import (AgentMessage, BackgroundTaskManager, DynamoDBBackgroundResultStore,
                                KiroAgentOptions, SQLiteBackgroundRequestStore, SQLiteBackgroundResultStore,
                                KiroEvent, TextBlock)
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
    from test_background_result_store import FakeDynamoDB


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreBackgroundArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.apps, self.managers = [], {}
        self.allowed = True

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, runtime_id):
        return self.allowed and req.headers.get("x-test-identity") == runtime_id

    def app(self, store, *, structured=True, timeout=5, automatic=False, events=False,
            history_events=8, history_bytes=16384, retry_store=None, overflow_policy="fail",
            checkpoints=False, checkpoint_interval=None):
        def managers(workspace, runtime_id):
            manager = BackgroundTaskManager(max_retained_result_bytes=4096, retain_structured_output=structured,
                overflow_policy=overflow_policy,
                max_history_events=history_events if events else 0,
                max_history_bytes=history_bytes if events else 0)
            self.managers[runtime_id] = manager
            return manager

        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / str(len(self.apps)), cli_path=str(FAKE),
            api_key="synthetic", allow_background_task_results=True, background_result_store_timeout=timeout,
            auto_archive_background_results=automatic, allow_sdk_events=events,
            allow_background_task_events=events, archive_background_task_events=events,
            allow_background_event_checkpoints=checkpoints,
            background_event_checkpoint_interval=checkpoint_interval,
            allow_background_task_retry=retry_store is not None)
        app = create_agentcore_app(config, session_authorizer=self.authorize, background_tasks_factory=managers,
            options_factory=lambda workspace, resume: KiroAgentOptions(tools=[]),
            background_task_options_factory=(lambda workspace, runtime_id: KiroAgentOptions(tools=[]))
                                            if automatic or retry_store is not None else None,
            background_request_store=retry_store,
            background_result_store=store)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, *, runtime_id="session-a", identity=None):
        return await request(app, "POST", "/invocations", json.dumps(payload).encode(),
            [(HEADER, runtime_id.encode()), (b"x-test-identity", (identity or runtime_id).encode())], timeout=15)

    async def connect(self, app, runtime_id="session-a"):
        self.assertEqual((await self.invoke(app, {"prompt": "connect"}, runtime_id=runtime_id))[0], 200)
        return self.managers[runtime_id]

    async def make_task(self, app, runtime_id="session-a"):
        manager = await self.connect(app, runtime_id)
        session = app.state.kiro_agentcore.sessions[runtime_id]
        task_id = await manager.start('{"ok": true, "private": "secret"}', options=KiroAgentOptions(
            cwd=session.directory / "workspace", cli_path=str(FAKE), agent_engine="v3",
            output_format={"type": "json_schema", "schema": {
                "type": "object", "properties": {"ok": {"type": "boolean"}, "private": {"type": "string"}},
                "required": ["ok", "private"]}}))
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        return manager, task_id

    async def test_sqlite_archive_recovers_only_for_authorized_session_after_replacement(self):
        path = self.root / "snapshots.sqlite"
        app = self.app(SQLiteBackgroundResultStore(path))
        manager, task_id = await self.make_task(app)
        payload = {"action": "background_task_archive", "task_id": task_id}
        self.assertEqual((await self.invoke(app, payload))[0], 200)
        self.assertEqual((await self.invoke(app, payload))[0], 200)
        manager.forget(task_id)
        await app.state.kiro_agentcore.close()
        replacement = self.app(SQLiteBackgroundResultStore(path))
        await self.connect(replacement)
        status, body, headers = await self.invoke(
            replacement, {"action": "background_task_archive_get", "task_id": task_id})
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        data = json.loads(body)
        self.assertEqual(data["task"]["state"], "completed")
        self.assertEqual(data["retained_result"]["structured_output"], {"ok": True, "private": "secret"})
        self.assertEqual((await self.invoke(replacement, {
            "action": "background_task_archive_get", "task_id": task_id}, identity="other"))[0], 403)
        await self.connect(replacement, "session-b")
        status, body, _ = await self.invoke(replacement, {
            "action": "background_task_archive_get", "task_id": task_id}, runtime_id="session-b")
        self.assertEqual(status, 404)
        self.assertNotIn(b"secret", body)

    async def test_incomplete_cleanup_blocks_result_archive_and_forget_until_stop_retry(self):
        from kiro_agent_sdk import ResultMessage
        store = SQLiteBackgroundResultStore(self.root / "cleanup.sqlite")
        app = self.app(store)
        manager = await self.connect(app)
        failed = True
        clients = []
        class Client(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="retained-private-result")
            async def disconnect(self):
                clients.append(self)
                if failed:
                    raise RuntimeError("private-cleanup-error")
                self.disconnected = True
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", Client):
            task_id = await manager.start("one execution")
            try:
                self.assertTrue((await manager.wait(task_id)).cleanup_pending)
                for action in ("background_task_result", "background_task_archive", "background_task_forget"):
                    status, body, _ = await self.invoke(app, {"action": action, "task_id": task_id})
                    self.assertEqual(status, 409, action)
                    self.assertNotIn(b"retained-private-result", body)
                    self.assertNotIn(b"private-cleanup-error", body)
                self.assertIsNone(await store.load("session-a", task_id))
                failed = False
                status, body, _ = await self.invoke(app, {"action": "background_task_stop", "task_id": task_id})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["task"]["state"], "failed")
                self.assertFalse(json.loads(body)["task"]["cleanup_pending"])
                self.assertEqual(len({id(client) for client in clients}), 1)
                self.assertTrue(clients[0].disconnected)
                self.assertEqual((await self.invoke(app, {
                    "action": "background_task_archive", "task_id": task_id}))[0], 200)
                stored = json.loads(await store.load("session-a", task_id))
                self.assertNotIn("cleanup_pending", stored["task"])
            finally:
                failed = False
                await manager.aclose()

    def test_archive_validator_accepts_previous_schema_but_rejects_unfinished_cleanup(self):
        from kiro_agent_sdk.agentcore import _background_archive_data
        task_id = "sdk-task-" + "a" * 32
        legacy = {"task_id": task_id, "state": "failed", "session_id": None,
                  "events_received": 0, "error_type": "RuntimeError", "events_dropped": 0}
        self.assertEqual(_background_archive_data(legacy, None, task_id, 0, False)["task"], legacy)
        self.assertEqual(_background_archive_data(
            {**legacy, "cleanup_pending": False}, None, task_id, 0, False)["task"], legacy)
        for pending in (True, 1, None, "false"):
            with self.subTest(pending=pending), self.assertRaises(ValueError):
                _background_archive_data({**legacy, "cleanup_pending": pending}, None, task_id, 0, False)
    async def test_mock_dynamodb_archive_across_instances(self):
        client = FakeDynamoDB()
        app = self.app(DynamoDBBackgroundResultStore("results-table", client=client))
        _, task_id = await self.make_task(app)
        self.assertEqual((await self.invoke(app, {"action": "background_task_archive", "task_id": task_id}))[0], 200)
        replacement = self.app(DynamoDBBackgroundResultStore("results-table", client=client))
        await self.connect(replacement)
        status, body, _ = await self.invoke(replacement, {
            "action": "background_task_archive_get", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["retained_result"]["structured_output"]["private"]),
                         (200, "secret"))

    async def test_event_archive_recovers_pages_and_gaps_only_for_authorized_owner(self):
        path = self.root / "events.sqlite"
        app = self.app(SQLiteBackgroundResultStore(path), events=True, history_events=1)
        manager, task_id = await self.make_task(app)
        expected = manager.event_history(task_id, after=0, limit=1)
        self.assertTrue(expected.gap)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive_events", "task_id": task_id}))[0], 200)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive", "task_id": task_id}))[0], 200)
        await app.state.kiro_agentcore.close()
        replacement = self.app(SQLiteBackgroundResultStore(path), events=True, history_events=1)
        await self.connect(replacement)
        payload = {"action": "background_task_archive_events_get", "task_id": task_id, "after": 0, "limit": 1}
        status, body, headers = await self.invoke(replacement, payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        page = json.loads(body)
        self.assertEqual(page["events"], expected.events)
        self.assertEqual((page["next_cursor"], page["latest_sequence"], page["history_dropped"],
                          page["gap"], page["has_more"]),
                         (expected.next_cursor, expected.latest_sequence, expected.history_dropped,
                          expected.gap, expected.has_more))
        self.assertEqual((await self.invoke(replacement, payload, identity="other"))[0], 403)
        self.assertEqual((await self.invoke(replacement, {**payload, "after": expected.latest_sequence + 1}))[0], 400)
        await self.connect(replacement, "session-b")
        self.assertEqual((await self.invoke(replacement, payload, runtime_id="session-b"))[0], 404)
        disabled = self.app(SQLiteBackgroundResultStore(path))
        await self.connect(disabled)
        self.assertEqual((await self.invoke(disabled, payload))[0], 403)
        self.assertEqual((await self.invoke(disabled, {
            "action": "background_task_archive_events_delete", "task_id": task_id}))[0], 200)
        self.assertEqual((await self.invoke(replacement, payload))[0], 404)
        self.assertEqual((await self.invoke(replacement, {
            "action": "background_task_archive_get", "task_id": task_id}))[0], 200)

    async def test_auto_archive_saves_separate_event_history(self):
        path = self.root / "automatic-events.sqlite"
        app = self.app(SQLiteBackgroundResultStore(path), automatic=True, events=True)
        manager = await self.connect(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_start",
            "request_id": "f" * 32, "prompt": "automatic event secret"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        expected = manager.event_history(task_id)
        await asyncio.wait_for(asyncio.gather(*list(app.state.kiro_agentcore._background_archive_watchers)), 5)
        await app.state.kiro_agentcore.close()
        replacement = self.app(SQLiteBackgroundResultStore(path), events=True)
        await self.connect(replacement)
        status, body, _ = await self.invoke(replacement, {
            "action": "background_task_archive_events_get", "task_id": task_id})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["events"], expected.events)

    async def test_running_checkpoint_recovery_and_independent_deletion(self):
        path = self.root / "checkpoints.sqlite"
        store = SQLiteBackgroundResultStore(path)
        app = self.app(store, events=True, checkpoints=True)
        manager = await self.connect(app)
        emitted, release = asyncio.Event(), asyncio.Event()

        class StagedClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage(content=[TextBlock(text="first private event")])
                emitted.set()
                await release.wait()
                yield AgentMessage(content=[TextBlock(text="second event")])

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", StagedClient):
            task_id = await manager.start("prompt")
            await asyncio.wait_for(emitted.wait(), 5)
            self.assertEqual(manager.status(task_id).state, "running")
            checkpoint = {"action": "background_task_checkpoint_events", "task_id": task_id}
            self.assertEqual((await self.invoke(app, checkpoint))[0], 200)
            self.assertEqual((await store.load_event_checkpoint("session-a", task_id))[0], 1)
            release.set()
            await manager.wait(task_id)
            self.assertEqual((await self.invoke(app, checkpoint))[0], 200)
            self.assertEqual((await self.invoke(app, {
                "action": "background_task_archive_events", "task_id": task_id}))[0], 200)
            self.assertEqual((await store.load_event_checkpoint("session-a", task_id))[0], 2)

        await app.state.kiro_agentcore.close()
        replacement = self.app(SQLiteBackgroundResultStore(path), events=True, checkpoints=True)
        await self.connect(replacement)
        get = {"action": "background_task_checkpoint_events_get", "task_id": task_id, "limit": 1}
        status, body, headers = await self.invoke(replacement, get)
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertTrue(json.loads(body)["has_more"])
        self.assertEqual((await self.invoke(replacement, get, identity="other"))[0], 403)
        await self.connect(replacement, "session-b")
        self.assertEqual((await self.invoke(replacement, get, runtime_id="session-b"))[0], 404)
        disabled = self.app(SQLiteBackgroundResultStore(path))
        await self.connect(disabled)
        self.assertEqual((await self.invoke(disabled, get))[0], 403)
        self.assertEqual((await self.invoke(disabled, {
            "action": "background_task_checkpoint_events_delete", "task_id": task_id}))[0], 200)
        self.assertEqual((await self.invoke(replacement, get))[0], 404)
        self.assertEqual((await self.invoke(replacement, {
            "action": "background_task_archive_events_get", "task_id": task_id}))[0], 200)
        self.assertFalse(await store.save_event_checkpoint("session-a", task_id, 3, b"late"))

    async def test_automatic_checkpoint_while_running_and_at_completion(self):
        store = SQLiteBackgroundResultStore(self.root / "automatic-checkpoints.sqlite")
        app = self.app(store, automatic=True, events=True, checkpoints=True, checkpoint_interval=0.1)
        manager = await self.connect(app)
        emitted, release = asyncio.Event(), asyncio.Event()

        class StagedClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage(content=[TextBlock(text="before")])
                emitted.set()
                await release.wait()
                yield AgentMessage(content=[TextBlock(text="after")])

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", StagedClient):
            status, body, _ = await self.invoke(app, {
                "action": "background_task_start", "request_id": "d" * 32, "prompt": "running"})
            self.assertEqual(status, 200)
            task_id = json.loads(body)["task_id"]
            await asyncio.wait_for(emitted.wait(), 5)
            async def wait_for_checkpoint():
                while await store.load_event_checkpoint("session-a", task_id) is None:
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(wait_for_checkpoint(), 5)
            self.assertEqual(manager.status(task_id).state, "running")
            self.assertEqual((await store.load_event_checkpoint("session-a", task_id))[0], 1)
            release.set()
            await manager.wait(task_id)
            await asyncio.wait_for(asyncio.gather(*list(app.state.kiro_agentcore._background_archive_watchers)), 5)
            self.assertEqual((await store.load_event_checkpoint("session-a", task_id))[0], 2)
            status, body, _ = await self.invoke(app, {
                "action": "background_task_checkpoint_events_get", "task_id": task_id})
            self.assertEqual((status, json.loads(body)["latest_sequence"]), (200, 2))

    async def test_deleted_checkpoint_does_not_prevent_final_auto_archive(self):
        class ObservingStore(SQLiteBackgroundResultStore):
            archived_on_conflict = False
            async def save_event_checkpoint(self, runtime_id, task_id, sequence, payload):
                result = await super().save_event_checkpoint(runtime_id, task_id, sequence, payload)
                if not result:
                    self.archived_on_conflict = (
                        await self.load(runtime_id, task_id) is not None and
                        await self.load_events(runtime_id, task_id) is not None)
                return result
        store = ObservingStore(self.root / "deleted-checkpoint.sqlite")
        app = self.app(store, automatic=True, events=True, checkpoints=True, checkpoint_interval=0.1, timeout=0.3)
        manager = await self.connect(app)
        emitted, release = asyncio.Event(), asyncio.Event()

        class StagedClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage(content=[TextBlock(text="before")])
                emitted.set()
                await release.wait()
                yield AgentMessage(content=[TextBlock(text="after")])

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", StagedClient):
            status, body, _ = await self.invoke(app, {
                "action": "background_task_start", "request_id": "e" * 32, "prompt": "running"})
            self.assertEqual(status, 200)
            task_id = json.loads(body)["task_id"]
            await asyncio.wait_for(emitted.wait(), 5)
            async def wait_for_checkpoint():
                while await store.load_event_checkpoint("session-a", task_id) is None:
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(wait_for_checkpoint(), 5)
            self.assertTrue(await store.delete_event_checkpoint("session-a", task_id))
            release.set()
            await manager.wait(task_id)
            await asyncio.wait_for(asyncio.gather(*list(app.state.kiro_agentcore._background_archive_watchers)), 5)
            self.assertIsNotNone(await store.load("session-a", task_id))
            self.assertIsNotNone(await store.load_events("session-a", task_id))
            self.assertIsNone(await store.load_event_checkpoint("session-a", task_id))
            self.assertTrue(store.archived_on_conflict)

    async def test_checkpoint_sequence_is_validated_before_output(self):
        class CorruptStore(SQLiteBackgroundResultStore):
            async def load_event_checkpoint(self, runtime_id, task_id):
                result = await super().load_event_checkpoint(runtime_id, task_id)
                return (result[0] + 1, result[1]) if result else None
        app = self.app(CorruptStore(self.root / "corrupt.sqlite"), events=True, checkpoints=True)
        _, task_id = await self.make_task(app)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_checkpoint_events", "task_id": task_id}))[0], 200)
        status, body, _ = await self.invoke(app, {
            "action": "background_task_checkpoint_events_get", "task_id": task_id})
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)

    async def test_checkpoint_late_save_cannot_undo_delete(self):
        class DelayedStore(SQLiteBackgroundResultStore):
            def __init__(self, path):
                super().__init__(path)
                self.delay = False
                self.entered, self.release = asyncio.Event(), asyncio.Event()
            async def save_event_checkpoint(self, *args):
                if self.delay:
                    self.entered.set()
                    await self.release.wait()
                return await super().save_event_checkpoint(*args)

        path = self.root / "late-checkpoint.sqlite"
        store = DelayedStore(path)
        app = self.app(store, events=True, checkpoints=True)
        _, task_id = await self.make_task(app)
        save = {"action": "background_task_checkpoint_events", "task_id": task_id}
        self.assertEqual((await self.invoke(app, save))[0], 200)
        replacement = self.app(SQLiteBackgroundResultStore(path), events=True, checkpoints=True)
        await self.connect(replacement)
        store.delay = True
        pending = asyncio.create_task(self.invoke(app, save))
        try:
            await asyncio.wait_for(store.entered.wait(), 5)
            self.assertEqual((await self.invoke(replacement, {
                "action": "background_task_checkpoint_events_delete", "task_id": task_id}))[0], 200)
            store.release.set()
            self.assertEqual((await asyncio.wait_for(pending, 5))[0], 409)
            self.assertIsNone(await store.load_event_checkpoint("session-a", task_id))
        finally:
            store.release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_checkpoint_read_reauthorizes_after_response_headers(self):
        store = SQLiteBackgroundResultStore(self.root / "checkpoint-guard.sqlite")
        app = self.app(store, events=True, checkpoints=True)
        _, task_id = await self.make_task(app)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_checkpoint_events", "task_id": task_id}))[0], 200)
        incoming = [{"type": "http.request", "body": json.dumps({
            "action": "background_task_checkpoint_events_get", "task_id": task_id}).encode(), "more_body": False}]
        sent, wait = [], asyncio.Event()
        async def receive():
            if incoming:
                return incoming.pop(0)
            await wait.wait()
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                self.allowed = False
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/invocations", "raw_path": b"/invocations",
                 "query_string": b"", "root_path": "", "headers": [(HEADER, b"session-a"),
                    (b"x-test-identity", b"session-a")], "server": ("localhost", 8080), "client": ("localhost", 1)}
        try:
            await asyncio.wait_for(app(scope, receive, send), 10)
            data = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
            self.assertNotIn(b"secret", data)
            self.assertIn(b"Session access denied", data)
        finally:
            self.allowed = True

    async def test_approved_retry_automatically_archives_result_and_events(self):
        result_path = self.root / "retry-results.sqlite"
        claim_path = self.root / "retry-claims.sqlite"
        claims = SQLiteBackgroundRequestStore(claim_path)
        await claims.claim("session-a", "1" * 32)
        app = self.app(SQLiteBackgroundResultStore(result_path), automatic=True, events=True, retry_store=claims)
        manager = await self.connect(app)
        retry = {"action": "background_task_retry", "previous_request_id": "1" * 32,
                 "request_id": "2" * 32, "prompt": "approved retry",
                 "acknowledge_side_effects": True}
        status, body, _ = await self.invoke(app, retry)
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        await asyncio.wait_for(asyncio.gather(*list(app.state.kiro_agentcore._background_archive_watchers)), 5)
        await app.state.kiro_agentcore.close()
        replacement = self.app(SQLiteBackgroundResultStore(result_path), automatic=True, events=True,
                               retry_store=SQLiteBackgroundRequestStore(claim_path))
        await self.connect(replacement)
        self.assertEqual((await self.invoke(replacement, retry))[0], 409)
        status, body, _ = await self.invoke(replacement, {
            "action": "background_task_archive_get", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["retained_result"]["result"]), (200, "approved retry"))
        status, body, _ = await self.invoke(replacement, {
            "action": "background_task_archive_events_get", "task_id": task_id})
        self.assertEqual(status, 200)
        self.assertGreater(len(json.loads(body)["events"]), 0)

    async def test_event_archive_pages_preserve_cursor_and_reauthorize_output(self):
        store = SQLiteBackgroundResultStore(self.root / "pages.sqlite")
        app = self.app(store, events=True)
        manager, task_id = await self.make_task(app)
        first_page = manager.event_history(task_id, limit=1)
        self.assertTrue(first_page.has_more)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive_events", "task_id": task_id}))[0], 200)
        status, body, _ = await self.invoke(app, {
            "action": "background_task_archive_events_get", "task_id": task_id, "limit": 1})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["events"], first_page.events)
        self.assertTrue(json.loads(body)["has_more"])
        next_page = manager.event_history(task_id, after=first_page.next_cursor, limit=1)
        status, body, _ = await self.invoke(app, {
            "action": "background_task_archive_events_get", "task_id": task_id,
            "after": first_page.next_cursor, "limit": 1})
        self.assertEqual((status, json.loads(body)["events"]), (200, next_page.events))

        incoming = [{"type": "http.request", "body": json.dumps({
            "action": "background_task_archive_events_get", "task_id": task_id}).encode(), "more_body": False}]
        sent, wait = [], asyncio.Event()
        async def receive():
            if incoming:
                return incoming.pop(0)
            await wait.wait()
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                self.allowed = False
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/invocations", "raw_path": b"/invocations",
                 "query_string": b"", "root_path": "", "headers": [(HEADER, b"session-a"),
                    (b"x-test-identity", b"session-a")], "server": ("localhost", 8080), "client": ("localhost", 1)}
        try:
            await asyncio.wait_for(app(scope, receive, send), 10)
            data = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
            self.assertNotIn(b"secret", data)
            self.assertIn(b"Session access denied", data)
        finally:
            self.allowed = True

    async def test_invalid_archived_event_is_not_returned(self):
        store = SQLiteBackgroundResultStore(self.root / "corrupt-events.sqlite")
        app = self.app(store, events=True)
        await self.connect(app)
        task_id = "sdk-task-" + "a" * 32
        bad_event = b'{"type":"Unsupported","message":{"text":"secret"}}'
        snapshot = {"task_id": task_id, "latest_sequence": 1, "history_dropped": 0,
                    "events": [[1, base64.b64encode(bad_event).decode("ascii")]]}
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertTrue(await store.save_events("session-a", task_id, encoded))
        status, body, _ = await self.invoke(app, {
            "action": "background_task_archive_events_get", "task_id": task_id})
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)

    async def test_oversized_event_archive_does_not_prevent_result_archive(self):
        class BurstClient(ControlledClient):
            gate = None
            disconnect_gate = None
            async def receive_response(self):
                yield AgentMessage([TextBlock("a" * 100000)])
                yield AgentMessage([TextBlock("b" * 100000)])
        store = SQLiteBackgroundResultStore(self.root / "large-events.sqlite")
        app = self.app(store, events=True, history_bytes=300000)
        manager = await self.connect(app)
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", BurstClient):
            task_id = await manager.start("hello")
            self.assertEqual((await manager.wait(task_id)).state, "completed")
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive_events", "task_id": task_id}))[0], 413)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive", "task_id": task_id}))[0], 200)

    async def test_event_archive_handles_maximum_history_and_cursor_gaps(self):
        class ManyClient(ControlledClient):
            gate = None
            disconnect_gate = None
            async def receive_response(self):
                for index in range(1030):
                    yield KiroEvent("notice", {"index": index})
        store = SQLiteBackgroundResultStore(self.root / "many-events.sqlite")
        app = self.app(store, events=True, history_events=1024, history_bytes=200000,
                       overflow_policy="drop-oldest")
        manager = await self.connect(app)
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ManyClient):
            task_id = await manager.start("hello")
            self.assertEqual((await manager.wait(task_id)).state, "completed")
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive_events", "task_id": task_id}))[0], 200)
        expected = manager.event_history(task_id, after=0, limit=64)
        self.assertTrue(expected.gap)
        self.assertEqual(expected.history_dropped, 6)
        status, body, _ = await self.invoke(app, {
            "action": "background_task_archive_events_get", "task_id": task_id, "after": 0, "limit": 64})
        self.assertEqual(status, 200)
        page = json.loads(body)
        self.assertEqual((page["events"], page["next_cursor"], page["history_dropped"], page["gap"], page["has_more"]),
                         (expected.events, expected.next_cursor, expected.history_dropped, expected.gap, True))

    async def test_mock_dynamodb_event_snapshot_is_independent(self):
        client = FakeDynamoDB()
        store = DynamoDBBackgroundResultStore("results-table", client=client)
        app = self.app(store, events=True)
        manager, task_id = await self.make_task(app)
        expected = manager.event_history(task_id)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive_events", "task_id": task_id}))[0], 200)
        replacement = self.app(DynamoDBBackgroundResultStore("results-table", client=client), events=True)
        await self.connect(replacement)
        status, body, _ = await self.invoke(replacement, {
            "action": "background_task_archive_events_get", "task_id": task_id})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["events"], expected.events)
        self.assertEqual((await self.invoke(replacement, {
            "action": "background_task_archive_events_delete", "task_id": task_id}))[0], 200)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive_events", "task_id": task_id}))[0], 409)

    async def test_event_delete_blocks_a_late_concurrent_save(self):
        class DelayedEventsStore:
            def __init__(self, inner):
                self.inner, self.calls = inner, 0
                self.entered, self.release = asyncio.Event(), asyncio.Event()
            async def save(self, *args): return await self.inner.save(*args)
            async def load(self, *args): return await self.inner.load(*args)
            async def save_events(self, *args):
                self.calls += 1
                if self.calls > 1:
                    self.entered.set()
                    await self.release.wait()
                return await self.inner.save_events(*args)
            async def load_events(self, *args): return await self.inner.load_events(*args)
            async def delete_events(self, *args): return await self.inner.delete_events(*args)

        path = self.root / "late-events.sqlite"
        store = DelayedEventsStore(SQLiteBackgroundResultStore(path))
        app = self.app(store, events=True)
        _, task_id = await self.make_task(app)
        save = {"action": "background_task_archive_events", "task_id": task_id}
        delete = {"action": "background_task_archive_events_delete", "task_id": task_id}
        self.assertEqual((await self.invoke(app, save))[0], 200)
        replacement = self.app(SQLiteBackgroundResultStore(path), events=True)
        await self.connect(replacement)
        pending = asyncio.create_task(self.invoke(app, save))
        try:
            await asyncio.wait_for(store.entered.wait(), 5)
            self.assertEqual((await self.invoke(replacement, delete))[0], 200)
            store.release.set()
            self.assertEqual((await asyncio.wait_for(pending, 5))[0], 409)
            self.assertEqual((await self.invoke(replacement, {
                "action": "background_task_archive_events_get", "task_id": task_id}))[0], 404)
        finally:
            store.release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_event_read_suppresses_data_when_authorization_changes_inflight(self):
        class DelayedEventsStore:
            def __init__(self, inner):
                self.inner = inner
                self.entered, self.release = asyncio.Event(), asyncio.Event()
            async def save(self, *args): return await self.inner.save(*args)
            async def load(self, *args): return await self.inner.load(*args)
            async def save_events(self, *args): return await self.inner.save_events(*args)
            async def load_events(self, *args):
                value = await self.inner.load_events(*args)
                self.entered.set()
                await self.release.wait()
                return value
            async def delete_events(self, *args): return await self.inner.delete_events(*args)

        store = DelayedEventsStore(SQLiteBackgroundResultStore(self.root / "revoked-events.sqlite"))
        app = self.app(store, events=True)
        _, task_id = await self.make_task(app)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive_events", "task_id": task_id}))[0], 200)
        pending = asyncio.create_task(self.invoke(app, {
            "action": "background_task_archive_events_get", "task_id": task_id}))
        try:
            await asyncio.wait_for(store.entered.wait(), 5)
            self.allowed = False
            store.release.set()
            status, body, _ = await asyncio.wait_for(pending, 5)
            self.assertEqual(status, 403)
            self.assertNotIn(b"secret", body)
        finally:
            self.allowed = True
            store.release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_cancelled_task_can_be_archived_without_a_result(self):
        app = self.app(SQLiteBackgroundResultStore(self.root / "cancelled-status.sqlite"))
        manager = await self.connect(app)
        session = app.state.kiro_agentcore.sessions["session-a"]
        task_id = await manager.start("cancel", options=KiroAgentOptions(
            cwd=session.directory / "workspace", cli_path=str(FAKE), agent_engine="v3"))
        await manager.stop(task_id)
        self.assertEqual((await manager.wait(task_id)).state, "cancelled")
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_archive", "task_id": task_id}))[0], 200)
        status, body, _ = await self.invoke(app, {
            "action": "background_task_archive_get", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["task"]["state"]), (200, "cancelled"))
        self.assertIsNone(json.loads(body)["retained_result"])

    async def test_delete_blocks_late_save_and_retrieval_across_runtime_instances(self):
        class DelayedStore:
            def __init__(self, inner):
                self.inner, self.calls = inner, 0
                self.entered, self.release = asyncio.Event(), asyncio.Event()
            async def save(self, runtime_id, task_id, value):
                self.calls += 1
                if self.calls > 1:
                    self.entered.set()
                    await self.release.wait()
                return await self.inner.save(runtime_id, task_id, value)
            async def load(self, runtime_id, task_id):
                return await self.inner.load(runtime_id, task_id)
            async def delete(self, runtime_id, task_id):
                return await self.inner.delete(runtime_id, task_id)

        path = self.root / "deletion.sqlite"
        store = DelayedStore(SQLiteBackgroundResultStore(path))
        first = self.app(store)
        _, task_id = await self.make_task(first)
        archive = {"action": "background_task_archive", "task_id": task_id}
        delete = {"action": "background_task_archive_delete", "task_id": task_id}
        fetch = {"action": "background_task_archive_get", "task_id": task_id}
        self.assertEqual((await self.invoke(first, archive))[0], 200)
        second = self.app(SQLiteBackgroundResultStore(path))
        await self.connect(second)
        pending = asyncio.create_task(self.invoke(first, archive))
        try:
            await asyncio.wait_for(store.entered.wait(), 5)
            self.assertEqual((await self.invoke(second, delete, identity="other"))[0], 403)
            self.assertEqual((await self.invoke(second, delete))[0], 200)
            store.release.set()
            self.assertEqual((await asyncio.wait_for(pending, 5))[0], 409)
            self.assertEqual((await self.invoke(second, fetch))[0], 404)
            self.assertEqual((await self.invoke(second, delete))[0], 200)
        finally:
            store.release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_auto_archive_remote_task_can_be_recovered_after_replacement(self):
        path = self.root / "automatic.sqlite"
        app = self.app(SQLiteBackgroundResultStore(path), automatic=True)
        manager = await self.connect(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_start",
            "request_id": "a" * 32, "prompt": "automatic private result"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        service = app.state.kiro_agentcore
        await asyncio.wait_for(asyncio.gather(*list(service._background_archive_watchers)), 5)
        await service.close()
        replacement = self.app(SQLiteBackgroundResultStore(path))
        await self.connect(replacement)
        status, body, _ = await self.invoke(replacement, {
            "action": "background_task_archive_get", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["retained_result"]["result"]),
                         (200, "automatic private result"))

    async def test_auto_archive_respects_revocation_at_completion(self):
        path = self.root / "revoked.sqlite"
        store = SQLiteBackgroundResultStore(path)
        app = self.app(store, automatic=True)
        manager = await self.connect(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_start",
            "request_id": "b" * 32, "prompt": "cancel"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        try:
            async def wait_for_worker():
                while manager.status(task_id).state == "pending":
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_worker(), 5)
            self.allowed = False
            await manager.stop(task_id)
            await asyncio.wait_for(asyncio.gather(
                *list(app.state.kiro_agentcore._background_archive_watchers)), 5)
            self.assertIsNone(await store.load("session-a", task_id))
        finally:
            self.allowed = True

    async def test_timed_out_save_can_finish_and_be_retried_idempotently(self):
        class SlowStore:
            def __init__(self, inner):
                self.inner = inner
                self.entered, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def save(self, runtime_id, task_id, value):
                self.entered.set()
                await self.release.wait()
                result = await self.inner.save(runtime_id, task_id, value)
                self.finished.set()
                return result
            async def load(self, runtime_id, task_id):
                return await self.inner.load(runtime_id, task_id)

        store = SlowStore(SQLiteBackgroundResultStore(self.root / "timeout.sqlite"))
        app = self.app(store, timeout=0.05)
        _, task_id = await self.make_task(app)
        payload = {"action": "background_task_archive", "task_id": task_id}
        try:
            status, _, _ = await self.invoke(app, payload)
            self.assertEqual(status, 503)
            self.assertTrue(store.entered.is_set())
            store.release.set()
            await asyncio.wait_for(store.finished.wait(), 5)
            self.assertEqual((await self.invoke(app, payload))[0], 200)
        finally:
            store.release.set()

    async def test_pending_store_operations_remain_bounded_after_timeout(self):
        class HungStore:
            def __init__(self):
                self.release = asyncio.Event()
                self.calls = 0
            async def save(self, *args):
                self.calls += 1
                await self.release.wait()
                return True
            async def load(self, *args):
                return None
        store = HungStore()
        app = self.app(store)
        service = app.state.kiro_agentcore
        try:
            operations = [service._background_archive_operation(store.save(), 0.05) for _ in range(16)]
            await asyncio.gather(*operations)
            self.assertEqual(len(service._background_archive_operations), 16)
            self.assertEqual(store.calls, 16)
            await service._background_archive_operation(store.save(), 0.05)
            self.assertEqual(store.calls, 16)
            store.release.set()
            await asyncio.gather(*list(service._background_archive_operations))
            await asyncio.sleep(0)
            self.assertEqual(len(service._background_archive_operations), 0)
        finally:
            store.release.set()

    async def test_cancelled_save_does_not_cancel_a_late_persistent_write(self):
        class SlowStore:
            def __init__(self, inner):
                self.inner = inner
                self.entered, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def save(self, runtime_id, task_id, value):
                self.entered.set()
                await self.release.wait()
                result = await self.inner.save(runtime_id, task_id, value)
                self.finished.set()
                return result
            async def load(self, runtime_id, task_id):
                return await self.inner.load(runtime_id, task_id)
        store = SlowStore(SQLiteBackgroundResultStore(self.root / "cancelled.sqlite"))
        app = self.app(store)
        _, task_id = await self.make_task(app)
        payload = {"action": "background_task_archive", "task_id": task_id}
        pending = asyncio.create_task(self.invoke(app, payload))
        try:
            await asyncio.wait_for(store.entered.wait(), 5)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            store.release.set()
            await asyncio.wait_for(store.finished.wait(), 5)
            self.assertEqual((await self.invoke(app, payload))[0], 200)
        finally:
            store.release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_revoked_authorization_and_changed_store_suppress_inflight_reads(self):
        class DelayedStore:
            def __init__(self, inner):
                self.inner = inner
                self.entered, self.release = asyncio.Event(), asyncio.Event()
            async def save(self, runtime_id, task_id, value):
                return await self.inner.save(runtime_id, task_id, value)
            async def load(self, runtime_id, task_id):
                value = await self.inner.load(runtime_id, task_id)
                self.entered.set()
                await self.release.wait()
                return value

        for change_store in (False, True):
            with self.subTest(change_store=change_store):
                store = DelayedStore(SQLiteBackgroundResultStore(self.root / f"store-{change_store}"))
                app = self.app(store)
                _, task_id = await self.make_task(app)
                self.assertEqual((await self.invoke(app, {
                    "action": "background_task_archive", "task_id": task_id}))[0], 200)
                pending = asyncio.create_task(self.invoke(app, {
                    "action": "background_task_archive_get", "task_id": task_id}))
                try:
                    await asyncio.wait_for(store.entered.wait(), 5)
                    if change_store:
                        app.state.kiro_agentcore.background_result_store = object()
                    else:
                        self.allowed = False
                    store.release.set()
                    status, body, _ = await asyncio.wait_for(pending, 5)
                    self.assertEqual(status, 403)
                    self.assertNotIn(b"secret", body)
                finally:
                    self.allowed = True
                    store.release.set()
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)

    async def test_read_body_is_reauthorized_after_headers(self):
        app = self.app(SQLiteBackgroundResultStore(self.root / "store.sqlite"))
        _, task_id = await self.make_task(app)
        self.assertEqual((await self.invoke(app, {"action": "background_task_archive", "task_id": task_id}))[0], 200)
        incoming = [{"type": "http.request", "body": json.dumps({
            "action": "background_task_archive_get", "task_id": task_id}).encode(), "more_body": False}]
        sent, wait = [], asyncio.Event()
        async def receive():
            if incoming:
                return incoming.pop(0)
            await wait.wait()
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                self.allowed = False
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/invocations", "raw_path": b"/invocations",
                 "query_string": b"", "root_path": "", "headers": [(HEADER, b"session-a"),
                    (b"x-test-identity", b"session-a")], "server": ("localhost", 8080), "client": ("localhost", 1)}
        try:
            await asyncio.wait_for(app(scope, receive, send), 10)
            body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
            self.assertNotIn(b"secret", body)
            self.assertIn(b"Session access denied", body)
        finally:
            self.allowed = True

    async def test_malformed_snapshot_is_never_returned(self):
        class BadStore:
            async def save(self, runtime_id, task_id, value):
                return True
            async def load(self, runtime_id, task_id):
                return b'{"private":"secret","task":{},"retained_result":null}'
        app = self.app(BadStore())
        await self.connect(app)
        status, body, _ = await self.invoke(app, {
            "action": "background_task_archive_get", "task_id": "sdk-task-" + "a" * 32})
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)

    def test_store_and_timeout_configuration_validation(self):
        class SyncStore:
            def save(self, *args): pass
            def load(self, *args): pass
        with self.assertRaises(ValueError):
            self.app(SyncStore())
        for value in (False, 0, -1, 61, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, background_result_store_timeout=value)
        with self.assertRaises(ValueError):
            create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, auto_archive_background_results=True))
        with self.assertRaises(ValueError):
            AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, api_key="synthetic", archive_background_task_events=True)
        with self.assertRaises(ValueError):
            AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, api_key="synthetic", archive_background_task_events="true")
        with self.assertRaises(ValueError):
            AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, allow_background_event_checkpoints=True)
        for value in (False, 0, -1, 0.09, 301, float("nan"), float("inf")):
            with self.subTest(interval=value), self.assertRaises(ValueError):
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, background_event_checkpoint_interval=value)
        with self.assertRaises(ValueError):
            AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, background_event_checkpoint_interval=1)
        class ResultOnlyStore:
            async def save(self, *args): return True
            async def load(self, *args): return None
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, api_key="synthetic",
                                 allow_sdk_events=True, allow_background_task_events=True,
                                 allow_background_task_results=True, archive_background_task_events=True)
        with self.assertRaises(ValueError):
            create_agentcore_app(config, session_authorizer=self.authorize,
                background_tasks_factory=lambda workspace, runtime_id: BackgroundTaskManager(),
                background_result_store=ResultOnlyStore())
        class EventsOnlyStore(ResultOnlyStore):
            async def save_events(self, *args): return True
            async def load_events(self, *args): return None
            async def delete_events(self, *args): return True
        checkpoint_config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, api_key="synthetic", allow_sdk_events=True,
            allow_background_task_events=True, allow_background_task_results=True,
            archive_background_task_events=True, allow_background_event_checkpoints=True)
        with self.assertRaises(ValueError):
            create_agentcore_app(checkpoint_config, session_authorizer=self.authorize,
                background_tasks_factory=lambda workspace, runtime_id: BackgroundTaskManager(),
                background_result_store=EventsOnlyStore())
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_SDK_EVENTS": "true",
                "KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_EVENTS": "true",
                "KIRO_AGENTCORE_ARCHIVE_BACKGROUND_TASK_EVENTS": "true",
                "KIRO_AGENTCORE_ALLOW_BACKGROUND_EVENT_CHECKPOINTS": "true",
                "KIRO_AGENTCORE_AUTO_ARCHIVE_BACKGROUND_RESULTS": "true",
                "KIRO_AGENTCORE_BACKGROUND_EVENT_CHECKPOINT_INTERVAL": "2"}, clear=True):
            config = AgentCoreConfig.from_env()
            self.assertTrue(config.archive_background_task_events)
            self.assertTrue(config.allow_background_event_checkpoints)
            self.assertEqual(config.background_event_checkpoint_interval, 2)
