import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request

if HAS_STARLETTE:
    from kiro_agent_sdk import (AgentDefinition, BackgroundTaskManager, DynamoDBBackgroundRequestStore,
                                HookMatcher, KiroAgentOptions, SQLiteBackgroundRequestStore)
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
    from test_background_request_store import FakeDynamoDB


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreBackgroundStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.apps = []
        self.managers = {}
        self.calls = 0
        self.allowed = True
        self.revoke_at = None
        self.log = self.root / "background-requests.jsonl"

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, runtime_id):
        self.calls += 1
        return (self.allowed and self.calls != self.revoke_at and
                req.headers.get("x-test-identity") == runtime_id)

    def app(self, *, start=True, options_factory=None, parent_factory=None, max_tasks=4,
            request_store=None, claim_timeout=5, retain_structured=False, retry=False, shutdown_timeout=30):
        def managers(workspace, runtime_id):
            manager = BackgroundTaskManager(max_tasks=max_tasks, max_events=32,
                overflow_policy="drop-oldest", max_retained_result_bytes=65536,
                retain_structured_output=retain_structured, shutdown_timeout=shutdown_timeout)
            self.managers[runtime_id] = manager
            return manager

        def defaults(workspace, runtime_id):
            return KiroAgentOptions(tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)})

        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / f"state-{len(self.apps)}", cli_path=str(FAKE),
                                 api_key="synthetic-test-key", allow_background_task_results=True,
                                 background_request_claim_timeout=claim_timeout, allow_background_task_retry=retry)
        app = create_agentcore_app(config, session_authorizer=self.authorize,
            options_factory=parent_factory or (lambda workspace, resume: KiroAgentOptions(tools=[])),
            background_tasks_factory=managers,
            background_task_options_factory=(options_factory or defaults) if start else None,
            background_request_store=request_store)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, runtime_id="session-a", identity=None, auto_request_id=True):
        if payload.get("action") == "background_task_start" and "request_id" not in payload and auto_request_id:
            payload = {**payload, "request_id": uuid.uuid4().hex}
        return await request(app, "POST", "/invocations", json.dumps(payload).encode(),
            [(HEADER, runtime_id.encode()), (b"x-test-identity", (identity or runtime_id).encode())], timeout=20)

    async def connect(self, app):
        status, body, _ = await self.invoke(app, {"prompt": "connect"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body).get("is_error", False))
        self.calls = 0
        return self.managers["session-a"]

    async def test_owner_starts_independent_task_and_reads_retained_result(self):
        app = self.app()
        manager = await self.connect(app)
        status, body, headers = await self.invoke(
            app, {"action": "background_task_start", "prompt": "hello background"})
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        task_id = json.loads(body)["task_id"]
        self.assertNotIn(b"synthetic-test-key", body)
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["result"]), (200, "hello background"))
        self.assertNotIn(b"synthetic-test-key", body)

    async def test_failed_cleanup_blocks_new_claims_and_retry_but_preserves_existing_receipt(self):
        from test_background_tasks import ControlledClient
        store = SQLiteBackgroundRequestStore(self.root / "cleanup-claims.sqlite")
        app = self.app(request_store=store, retry=True)
        manager = await self.connect(app)
        failed = True
        clients = []
        class Client(ControlledClient):
            async def receive_response(self):
                if False:
                    yield None
            async def disconnect(self):
                clients.append(self)
                if failed:
                    raise RuntimeError("private cleanup failure")
                self.disconnected = True
        first = {"action": "background_task_start", "request_id": "a" * 32, "prompt": "one execution"}
        retry = {"action": "background_task_retry", "request_id": "c" * 32, "previous_request_id": "a" * 32,
                 "prompt": "explicit new execution", "acknowledge_side_effects": True}
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", Client):
            status, body, _ = await self.invoke(app, first)
            self.assertEqual(status, 200)
            task_id = json.loads(body)["task_id"]
            try:
                self.assertTrue((await manager.wait(task_id)).cleanup_pending)
                status, body, _ = await self.invoke(app, first)
                self.assertEqual((status, json.loads(body)["task_id"]), (200, task_id))
                for payload in ({**first, "request_id": "b" * 32}, retry):
                    status, body, _ = await self.invoke(app, payload)
                    self.assertEqual(status, 409)
                    self.assertNotIn(b"private cleanup failure", body)
                    self.assertFalse(await store.exists("session-a", payload["request_id"]))
                failed = False
                status, body, _ = await self.invoke(app, {"action": "background_task_stop", "task_id": task_id})
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(body)["task"]["cleanup_pending"])
                self.assertEqual(len({id(client) for client in clients}), 1)
                status, body, _ = await self.invoke(app, retry)
                self.assertEqual(status, 200)
                retry_id = json.loads(body)["task_id"]
                self.assertNotEqual(task_id, retry_id)
                self.assertEqual((await manager.wait(retry_id)).state, "completed")
            finally:
                failed = False
                await manager.aclose()

    async def test_remote_start_with_agent_initial_prompt_returns_the_requested_result(self):
        app = self.app(options_factory=lambda workspace, runtime_id: KiroAgentOptions(
            agent="selected",
            agents={"selected": AgentDefinition("Test", "Test", initial_prompt="initial")}))
        manager = await self.connect(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "requested"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["result"]), (200, "requested"))

    async def test_start_requires_opt_in_and_session_owner(self):
        app = self.app(start=False)
        manager = await self.connect(app)
        status, _, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "hello"})
        self.assertEqual(status, 403)
        self.assertEqual(manager._tasks, {})
        app = self.app()
        manager = await self.connect(app)
        status, _, _ = await self.invoke(
            app, {"action": "background_task_start", "prompt": "hello"}, identity="other")
        self.assertEqual(status, 403)
        self.assertEqual(manager._tasks, {})

    async def test_start_request_id_is_required_and_retries_do_not_duplicate_work(self):
        app = self.app()
        manager = await self.connect(app)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_start", "prompt": "hello"}, auto_request_id=False))[0], 400)
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_start", "prompt": "hello", "request_id": "invalid"}))[0], 400)
        payload = {"action": "background_task_start", "prompt": "hello", "request_id": "a" * 32}
        status, body, _ = await self.invoke(app, payload)
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        status, body, _ = await self.invoke(app, payload)
        self.assertEqual((status, json.loads(body)["task_id"]), (200, task_id))
        await manager.wait(task_id)
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        prompts = [item for item in requests if item.get("method") == "session/prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual((await self.invoke(app, {**payload, "prompt": "different"}))[0], 409)
        self.assertEqual((await self.invoke(app, {"action": "background_task_forget", "task_id": task_id}))[0], 200)
        self.assertEqual((await self.invoke(app, payload))[0], 409)
        self.assertEqual(manager._tasks, {})

    async def test_retry_requires_opt_in_distinct_ids_and_explicit_acknowledgement(self):
        store = SQLiteBackgroundRequestStore(self.root / "retry.sqlite")
        app = self.app(request_store=store)
        manager = await self.connect(app)
        original = "a" * 32
        await store.claim("session-a", original)
        payload = {"action": "background_task_retry", "previous_request_id": original,
                   "request_id": "b" * 32, "prompt": "hello", "acknowledge_side_effects": True}
        self.assertEqual((await self.invoke(app, payload))[0], 403)
        self.assertEqual(manager._tasks, {})
        app = self.app(request_store=store, retry=True)
        manager = await self.connect(app)
        for invalid in ({**payload, "acknowledge_side_effects": False},
                        {**payload, "acknowledge_side_effects": 1},
                        {key: value for key, value in payload.items() if key != "acknowledge_side_effects"},
                        {**payload, "request_id": original},
                        {**payload, "previous_request_id": "../bad"}):
            with self.subTest(payload=invalid):
                self.assertEqual((await self.invoke(app, invalid))[0], 400)
        self.assertEqual((await self.invoke(app, payload, identity="other"))[0], 403)
        self.assertEqual((await self.invoke(app, {**payload, "previous_request_id": "c" * 32}))[0], 404)
        self.assertEqual(manager._tasks, {})

    async def test_retry_of_cancelled_local_task_is_idempotent_and_blocks_active_or_successful_sources(self):
        app = self.app(request_store=SQLiteBackgroundRequestStore(self.root / "retry.sqlite"), retry=True)
        manager = await self.connect(app)
        original = "c" * 32
        status, body, _ = await self.invoke(app, {
            "action": "background_task_start", "request_id": original, "prompt": "cancel"})
        self.assertEqual(status, 200)
        old_task = json.loads(body)["task_id"]
        retry = {"action": "background_task_retry", "previous_request_id": original,
                 "request_id": "d" * 32, "prompt": "retry", "acknowledge_side_effects": True}
        self.assertEqual((await self.invoke(app, retry))[0], 409)
        await manager.stop(old_task)
        self.assertEqual((await manager.wait(old_task)).state, "cancelled")
        status, body, _ = await self.invoke(app, retry)
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        status, body, _ = await self.invoke(app, retry)
        self.assertEqual((status, json.loads(body)["task_id"]), (200, task_id))
        self.assertEqual((await self.invoke(app, {**retry, "previous_request_id": "e" * 32}))[0], 409)
        self.assertEqual((await self.invoke(app, {"action": "background_task_start",
            "request_id": retry["request_id"], "prompt": "retry"}))[0], 409)
        self.assertEqual((await self.invoke(app, {**retry, "previous_request_id": retry["request_id"],
                                                 "request_id": "e" * 32}))[0], 409)

    async def test_retry_after_runtime_replacement_requires_prior_session_claim(self):
        path = self.root / "retry.sqlite"
        app = self.app(request_store=SQLiteBackgroundRequestStore(path), retry=True)
        manager = await self.connect(app)
        original = "f" * 32
        status, body, _ = await self.invoke(app, {
            "action": "background_task_start", "request_id": original, "prompt": "original"})
        self.assertEqual(status, 200)
        await manager.wait(json.loads(body)["task_id"])
        await app.state.kiro_agentcore.close()
        replacement = self.app(request_store=SQLiteBackgroundRequestStore(path), retry=True)
        manager = await self.connect(replacement)
        retry = {"action": "background_task_retry", "previous_request_id": original,
                 "request_id": "a" * 32, "prompt": "retry", "acknowledge_side_effects": True}
        status, body, _ = await self.invoke(replacement, retry)
        self.assertEqual(status, 200)
        self.assertEqual((await manager.wait(json.loads(body)["task_id"])).state, "completed")
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len([item for item in requests if item.get("method") == "session/prompt"]), 2)

    async def test_two_instances_cannot_duplicate_same_approved_retry(self):
        path = self.root / "retry.sqlite"
        store = SQLiteBackgroundRequestStore(path)
        await store.claim("session-a", "1" * 32)
        first = self.app(request_store=store, retry=True)
        first_manager = await self.connect(first)
        second = self.app(request_store=SQLiteBackgroundRequestStore(path), retry=True)
        second_manager = await self.connect(second)
        payload = {"action": "background_task_retry", "previous_request_id": "1" * 32,
                   "request_id": "2" * 32, "prompt": "retry", "acknowledge_side_effects": True}
        results = await asyncio.gather(self.invoke(first, payload), self.invoke(second, payload))
        self.assertEqual(sorted(item[0] for item in results), [200, 409])
        winner = next(json.loads(body)["task_id"] for status, body, _ in results if status == 200)
        manager = first_manager if first_manager.owns(winner) else second_manager
        self.assertEqual((await manager.wait(winner)).state, "completed")
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len([item for item in requests if item.get("method") == "session/prompt"]), 1)

    async def test_retry_checks_mock_dynamodb_with_a_consistent_session_scoped_read(self):
        client = FakeDynamoDB()
        store = DynamoDBBackgroundRequestStore("claims-table", client=client)
        await store.claim("session-b", "1" * 32)
        app = self.app(request_store=store, retry=True)
        manager = await self.connect(app)
        payload = {"action": "background_task_retry", "previous_request_id": "1" * 32,
                   "request_id": "2" * 32, "prompt": "retry", "acknowledge_side_effects": True}
        self.assertEqual((await self.invoke(app, payload))[0], 404)
        self.assertEqual(manager._tasks, {})
        await store.claim("session-a", "1" * 32)
        status, body, _ = await self.invoke(app, payload)
        self.assertEqual(status, 200)
        self.assertEqual((await manager.wait(json.loads(body)["task_id"])).state, "completed")
        reads = [call for call in client.calls if "Key" in call]
        self.assertTrue(reads)
        self.assertTrue(all(call["ConsistentRead"] for call in reads))
        self.assertTrue(all("session-a" not in repr(call) and "1" * 32 not in repr(call) for call in reads))

    async def test_retry_rechecks_authorization_after_source_lookup(self):
        class DelayedStore:
            def __init__(self, inner):
                self.inner = inner
                self.entered, self.release = asyncio.Event(), asyncio.Event()
            async def claim(self, runtime_id, request_id):
                return await self.inner.claim(runtime_id, request_id)
            async def exists(self, runtime_id, request_id):
                result = await self.inner.exists(runtime_id, request_id)
                self.entered.set()
                await self.release.wait()
                return result
        for replace_store in (False, True):
            with self.subTest(replace_store=replace_store):
                inner = SQLiteBackgroundRequestStore(self.root / f"retry-{replace_store}.sqlite")
                await inner.claim("session-a", "3" * 32)
                store = DelayedStore(inner)
                app = self.app(request_store=store, retry=True)
                manager = await self.connect(app)
                payload = {"action": "background_task_retry", "previous_request_id": "3" * 32,
                           "request_id": "4" * 32, "prompt": "must not run", "acknowledge_side_effects": True}
                pending = asyncio.create_task(self.invoke(app, payload))
                try:
                    await asyncio.wait_for(store.entered.wait(), 5)
                    if replace_store:
                        app.state.kiro_agentcore.background_request_store = SQLiteBackgroundRequestStore(
                            self.root / "replacement.sqlite")
                    else:
                        self.allowed = False
                    store.release.set()
                    status, body, _ = await asyncio.wait_for(pending, 5)
                    self.assertEqual(status, 403)
                    self.assertNotIn(b"sdk-task-", body)
                    self.assertEqual(manager._tasks, {})
                    self.assertFalse(await inner.exists("session-a", "4" * 32))
                finally:
                    self.allowed = True
                    store.release.set()
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)

    async def test_retry_lookup_timeout_never_starts_a_late_model_request(self):
        class DelayedStore:
            def __init__(self, inner):
                self.inner = inner
                self.entered, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def claim(self, runtime_id, request_id):
                return await self.inner.claim(runtime_id, request_id)
            async def exists(self, runtime_id, request_id):
                self.entered.set()
                await self.release.wait()
                result = await self.inner.exists(runtime_id, request_id)
                self.finished.set()
                return result
        inner = SQLiteBackgroundRequestStore(self.root / "retry.sqlite")
        await inner.claim("session-a", "5" * 32)
        store = DelayedStore(inner)
        app = self.app(request_store=store, retry=True, claim_timeout=0.05)
        manager = await self.connect(app)
        payload = {"action": "background_task_retry", "previous_request_id": "5" * 32,
                   "request_id": "6" * 32, "prompt": "retry", "acknowledge_side_effects": True}
        try:
            status, body, _ = await self.invoke(app, payload)
            self.assertEqual(status, 503)
            self.assertNotIn(b"sdk-task-", body)
            self.assertTrue(store.entered.is_set())
            self.assertEqual(manager._tasks, {})
            store.release.set()
            await asyncio.wait_for(store.finished.wait(), 5)
            self.assertFalse(await inner.exists("session-a", "6" * 32))
            status, body, _ = await self.invoke(app, payload)
            self.assertEqual(status, 200)
            self.assertEqual((await manager.wait(json.loads(body)["task_id"])).state, "completed")
        finally:
            store.release.set()

    async def test_retry_lookup_failure_and_invalid_result_are_sanitized(self):
        class BadStore:
            def __init__(self, error):
                self.error, self.claim_calls = error, 0
            async def claim(self, runtime_id, request_id):
                self.claim_calls += 1
                return True
            async def exists(self, runtime_id, request_id):
                if self.error:
                    raise RuntimeError("private-lookup-failure")
                return 1
        for error in (False, True):
            with self.subTest(error=error):
                store = BadStore(error)
                app = self.app(request_store=store, retry=True)
                manager = await self.connect(app)
                status, body, _ = await self.invoke(app, {
                    "action": "background_task_retry", "previous_request_id": "7" * 32,
                    "request_id": "8" * 32, "prompt": "must not run", "acknowledge_side_effects": True})
                self.assertEqual(status, 500)
                self.assertNotIn(b"private-lookup-failure", body)
                self.assertEqual(store.claim_calls, 0)
                self.assertEqual(manager._tasks, {})

    async def test_persistent_claim_prevents_duplicate_start_after_runtime_replacement(self):
        path = self.root / "requests.sqlite"
        app = self.app(request_store=SQLiteBackgroundRequestStore(path))
        manager = await self.connect(app)
        payload = {"action": "background_task_start", "prompt": "hello", "request_id": "d" * 32}
        status, body, _ = await self.invoke(app, payload)
        self.assertEqual(status, 200)
        await manager.wait(json.loads(body)["task_id"])
        await app.state.kiro_agentcore.close()
        replacement = self.app(request_store=SQLiteBackgroundRequestStore(path))
        manager = await self.connect(replacement)
        status, body, _ = await self.invoke(replacement, payload)
        self.assertEqual(status, 409)
        self.assertNotIn(b"sdk-task-", body)
        self.assertEqual(manager._tasks, {})
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len([item for item in requests if item.get("method") == "session/prompt"]), 1)

    async def test_persistent_claim_is_atomic_across_runtime_instances(self):
        path = self.root / "requests.sqlite"
        first = self.app(request_store=SQLiteBackgroundRequestStore(path))
        first_manager = await self.connect(first)
        second = self.app(request_store=SQLiteBackgroundRequestStore(path))
        second_manager = await self.connect(second)
        payload = {"action": "background_task_start", "prompt": "hello", "request_id": "e" * 32}
        results = await asyncio.gather(self.invoke(first, payload), self.invoke(second, payload))
        self.assertEqual(sorted(item[0] for item in results), [200, 409])
        winner = next(json.loads(body)["task_id"] for status, body, _ in results if status == 200)
        manager = first_manager if first_manager.owns(winner) else second_manager
        self.assertEqual((await manager.wait(winner)).state, "completed")
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len([item for item in requests if item.get("method") == "session/prompt"]), 1)

    async def test_dynamodb_claim_is_atomic_across_runtime_instances(self):
        client = FakeDynamoDB()
        first = self.app(request_store=DynamoDBBackgroundRequestStore("claims-table", client=client))
        first_manager = await self.connect(first)
        second = self.app(request_store=DynamoDBBackgroundRequestStore("claims-table", client=client))
        second_manager = await self.connect(second)
        payload = {"action": "background_task_start", "prompt": "hello", "request_id": "b" * 32}
        results = await asyncio.gather(self.invoke(first, payload), self.invoke(second, payload))
        self.assertEqual(sorted(item[0] for item in results), [200, 409])
        winner = next(json.loads(body)["task_id"] for status, body, _ in results if status == 200)
        manager = first_manager if first_manager.owns(winner) else second_manager
        self.assertEqual((await manager.wait(winner)).state, "completed")
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len([item for item in requests if item.get("method") == "session/prompt"]), 1)

    async def test_claimed_start_returns_structured_result_and_rejects_replay_after_replacement(self):
        path = self.root / "requests.sqlite"
        def options(workspace, runtime_id):
            return KiroAgentOptions(tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)},
                output_format={"type": "json_schema", "schema": {
                    "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}})

        app = self.app(request_store=SQLiteBackgroundRequestStore(path), options_factory=options,
                       retain_structured=True)
        manager = await self.connect(app)
        payload = {"action": "background_task_start", "prompt": '{"ok": true}', "request_id": "f" * 32}
        status, body, _ = await self.invoke(app, payload)
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["structured_output"]), (200, {"ok": True}))
        self.assertEqual((await self.invoke(app, payload))[0], 200)
        await app.state.kiro_agentcore.close()
        replacement = self.app(request_store=SQLiteBackgroundRequestStore(path), options_factory=options,
                               retain_structured=True)
        await self.connect(replacement)
        self.assertEqual((await self.invoke(replacement, payload))[0], 409)
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len([item for item in requests if item.get("method") == "session/prompt"]), 1)

    async def test_pending_claims_remain_bounded_after_timeout_and_cancellation(self):
        class DelayedStore:
            def __init__(self):
                self.entered, self.release = asyncio.Event(), asyncio.Event()
                self.calls = 0

            async def claim(self, runtime_id, request_id):
                self.calls += 1
                self.entered.set()
                await self.release.wait()
                return True

        store = DelayedStore()
        app = self.app(request_store=store)
        service = app.state.kiro_agentcore
        try:
            results = await asyncio.gather(*(service._claim_background_request(
                store, "session-a", f"{index:032x}", 0.05) for index in range(16)))
            self.assertEqual(results, [None] * 16)
            self.assertEqual(len(service._background_claims), 16)
            self.assertIsNone(await service._claim_background_request(store, "session-a", "f" * 32, 0.05))
            self.assertEqual(store.calls, 16)
            store.release.set()
            await asyncio.gather(*list(service._background_claims))
            await asyncio.sleep(0)
            self.assertEqual(len(service._background_claims), 0)
        finally:
            store.release.set()

    async def test_store_replacement_during_claim_fails_closed(self):
        class DelayedStore:
            def __init__(self):
                self.entered, self.release = asyncio.Event(), asyncio.Event()

            async def claim(self, runtime_id, request_id):
                self.entered.set()
                await self.release.wait()
                return True

        store = DelayedStore()
        app = self.app(request_store=store)
        manager = await self.connect(app)
        pending = asyncio.create_task(self.invoke(app, {
            "action": "background_task_start", "prompt": "must not run", "request_id": "c" * 32}))
        try:
            await asyncio.wait_for(store.entered.wait(), 5)
            app.state.kiro_agentcore.background_request_store = DelayedStore()
            store.release.set()
            status, _, _ = await asyncio.wait_for(pending, 5)
            self.assertEqual(status, 403)
            self.assertEqual(manager._tasks, {})
            self.assertFalse(self.log.exists())
        finally:
            store.release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_cancelled_start_does_not_cancel_claim_or_start_the_model(self):
        class DelayedStore:
            def __init__(self):
                self.entered, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
                self.claimed = False

            async def claim(self, runtime_id, request_id):
                self.entered.set()
                await self.release.wait()
                fresh = not self.claimed
                self.claimed = True
                self.finished.set()
                return fresh

        store = DelayedStore()
        app = self.app(request_store=store)
        manager = await self.connect(app)
        payload = {"action": "background_task_start", "prompt": "must not run", "request_id": "d" * 32}
        pending = asyncio.create_task(self.invoke(app, payload))
        try:
            await asyncio.wait_for(store.entered.wait(), 5)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            store.release.set()
            await asyncio.wait_for(store.finished.wait(), 5)
            self.assertEqual((await self.invoke(app, payload))[0], 409)
            self.assertEqual(manager._tasks, {})
            self.assertFalse(self.log.exists())
        finally:
            store.release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    def test_claim_store_and_timeout_configuration_validation(self):
        class SyncStore:
            def claim(self, runtime_id, request_id):
                return True

        with self.assertRaises(ValueError):
            self.app(request_store=SyncStore())
        with self.assertRaises(ValueError):
            self.app(start=False, request_store=SQLiteBackgroundRequestStore(self.root / "claims"))
        for value in (False, 0, -1, 61, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, background_request_claim_timeout=value)
        class ClaimOnly:
            async def claim(self, runtime_id, request_id): return True
        with self.assertRaises(ValueError):
            self.app(request_store=ClaimOnly(), retry=True)
        with self.assertRaises(ValueError):
            self.app(retry=True)
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic",
                                     "KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_RETRY": "true"}, clear=True):
            self.assertTrue(AgentCoreConfig.from_env().allow_background_task_retry)

    async def test_store_error_or_invalid_return_fails_closed_without_disclosing_details(self):
        class BadStore:
            def __init__(self, error):
                self.error = error

            async def claim(self, runtime_id, request_id):
                if self.error:
                    raise RuntimeError("private-store-error")
                return 1

        for error in (False, True):
            with self.subTest(error=error):
                app = self.app(request_store=BadStore(error))
                manager = await self.connect(app)
                status, body, _ = await self.invoke(app, {
                    "action": "background_task_start", "prompt": "must not run", "request_id": "e" * 32})
                self.assertEqual(status, 500)
                self.assertNotIn(b"private-store-error", body)
                self.assertEqual(manager._tasks, {})
                self.assertFalse(self.log.exists())

    async def test_timeout_or_revocation_during_claim_never_starts_the_model(self):
        class DelayedStore:
            def __init__(self):
                self.entered, self.release, self.finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
                self.claimed = set()
            async def claim(self, runtime_id, request_id):
                self.entered.set()
                await self.release.wait()
                key = (runtime_id, request_id)
                fresh = key not in self.claimed
                self.claimed.add(key)
                self.finished.set()
                return fresh

        for revoke in (False, True):
            with self.subTest(revoke=revoke):
                store = DelayedStore()
                app = self.app(request_store=store, claim_timeout=1 if revoke else 0.05)
                manager = await self.connect(app)
                payload = {"action": "background_task_start", "prompt": "must not run",
                           "request_id": ("f" if revoke else "a") * 32}
                pending = asyncio.create_task(self.invoke(app, payload))
                try:
                    await asyncio.wait_for(store.entered.wait(), 5)
                    if revoke:
                        self.allowed = False
                        store.release.set()
                    status, body, _ = await asyncio.wait_for(pending, 5)
                    self.assertEqual(status, 403 if revoke else 503)
                    self.assertNotIn(b"sdk-task-", body)
                    self.assertEqual(manager._tasks, {})
                    store.release.set()
                    await asyncio.wait_for(store.finished.wait(), 5)
                    self.allowed = True
                    self.assertEqual((await self.invoke(app, payload))[0], 409)
                    self.assertFalse(self.log.exists())
                finally:
                    self.allowed = True
                    store.release.set()
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)

    async def test_retry_during_pending_start_does_not_start_a_second_task(self):
        entered, release = asyncio.Event(), asyncio.Event()
        app = self.app()
        manager = await self.connect(app)
        async def authorize(req, runtime_id):
            self.calls += 1
            if self.calls == 4:
                entered.set()
                await release.wait()
            return True
        app.state.kiro_agentcore.session_authorizer = authorize
        payload = {"action": "background_task_start", "prompt": "hello", "request_id": "b" * 32}
        first = asyncio.create_task(self.invoke(app, payload))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.assertEqual((await self.invoke(app, payload))[0], 409)
            release.set()
            status, body, _ = await first
            self.assertEqual(status, 200)
            task_id = json.loads(body)["task_id"]
            status, body, _ = await self.invoke(app, payload)
            self.assertEqual((status, json.loads(body)["task_id"]), (200, task_id))
            await manager.wait(task_id)
            self.assertEqual(len(manager._tasks), 1)
        finally:
            release.set()
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)

    async def test_invalid_prompts_and_session_overrides_never_start_tasks(self):
        app = self.app(options_factory=lambda workspace, runtime_id: KiroAgentOptions(resume="other"))
        manager = await self.connect(app)
        for prompt in ("", [{"type": "resource", "uri": "file:///secret"}]):
            self.assertEqual((await self.invoke(app, {"action": "background_task_start", "prompt": prompt}))[0], 400)
        status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "hello"})
        self.assertEqual(status, 500)
        self.assertNotIn(b"other", body)
        self.assertEqual(manager._tasks, {})
        self.assertFalse(self.log.exists())

    async def test_parent_authentication_environment_is_not_overridden(self):
        app = self.app(options_factory=lambda workspace, runtime_id: KiroAgentOptions(
            tools=[], env={"KIRO_API_KEY": "wrong", "KIRO_HOME": "/incorrect", "AWS_PROFILE": "wrong"}),
            parent_factory=lambda workspace, resume: KiroAgentOptions(
                tools=[], env={"AWS_PROFILE": "parent-profile"}))
        manager = await self.connect(app)
        service = app.state.kiro_agentcore
        parent = service.sessions["session-a"].client
        original = BackgroundTaskManager._start_guarded
        captured = {}

        async def inspect_options(current, prompt, options, guard):
            captured.update(options.env)
            return await original(current, prompt, options, guard)

        with patch.object(BackgroundTaskManager, "_start_guarded", new=inspect_options):
            status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "env_auth_probe"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        self.assertEqual(captured["KIRO_API_KEY"], parent.options.env["KIRO_API_KEY"])
        self.assertEqual(captured["KIRO_HOME"], parent.options.env["KIRO_HOME"])
        self.assertEqual(captured["AWS_PROFILE"], "parent-profile")
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["result"]), (200, "auth-present"))
        self.assertNotIn(b"synthetic-test-key", body)

    async def test_factory_cannot_inject_a_different_aws_identity(self):
        app = self.app(options_factory=lambda workspace, runtime_id: KiroAgentOptions(
            tools=[], env={"AWS_ACCESS_KEY_ID": "different-credential"}))
        manager = await self.connect(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "hello"})
        self.assertEqual(status, 500)
        self.assertNotIn(b"different-credential", body)
        self.assertEqual(manager._tasks, {})

    async def test_device_authenticated_session_reuses_home_without_api_key(self):
        def manager_factory(workspace, runtime_id):
            manager = BackgroundTaskManager(max_retained_result_bytes=256)
            self.managers[runtime_id] = manager
            return manager
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "device", cli_path=str(FAKE), auth_mode="device",
                                 allow_background_task_results=True)
        app = create_agentcore_app(config, session_authorizer=self.authorize,
            options_factory=lambda workspace, resume: KiroAgentOptions(tools=[]),
            background_tasks_factory=manager_factory,
            background_task_options_factory=lambda workspace, runtime_id: KiroAgentOptions(tools=[]))
        self.apps.append(app)
        with patch("kiro_agent_sdk.agentcore.cli_auth_command", return_value=True):
            manager = await self.connect(app)
            parent = app.state.kiro_agentcore.sessions["session-a"].client
            captured = {}
            original = BackgroundTaskManager._start_guarded
            async def inspect_options(current, prompt, options, guard):
                captured.update(options.env)
                return await original(current, prompt, options, guard)
            with patch.object(BackgroundTaskManager, "_start_guarded", new=inspect_options):
                status, body, _ = await self.invoke(app, {
                    "action": "background_task_start", "prompt": "env_auth_probe"})
            self.assertEqual(status, 200)
            task_id = json.loads(body)["task_id"]
            self.assertEqual(captured["KIRO_HOME"], parent.options.env["KIRO_HOME"])
            self.assertEqual(captured["XDG_RUNTIME_DIR"], parent.options.env["XDG_RUNTIME_DIR"])
            self.assertEqual(captured["KIRO_API_KEY"], "")
            self.assertEqual((await manager.wait(task_id)).state, "completed")
            status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
            self.assertEqual((status, json.loads(body)["result"]), (200, "auth-absent"))

    async def test_revocation_after_scheduling_cancels_before_worker_starts(self):
        app = self.app()
        manager = await self.connect(app)
        self.revoke_at = 4
        payload = {"action": "background_task_start", "prompt": "must not run", "request_id": "c" * 32}
        status, body, _ = await self.invoke(app, payload)
        self.assertEqual(status, 403)
        self.assertNotIn(b"sdk-task-", body)
        self.assertEqual(manager._tasks, {})
        self.assertFalse(self.log.exists())
        self.revoke_at = None
        status, body, _ = await self.invoke(app, payload)
        self.assertEqual(status, 200)
        await manager.wait(json.loads(body)["task_id"])
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len([item for item in requests if item.get("method") == "session/prompt"]), 1)

    async def test_revocation_before_worker_connection_prevents_model_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        app = self.app()
        manager = await self.connect(app)

        async def authorize(req, runtime_id):
            self.calls += 1
            if self.calls == 5:
                entered.set()
                await release.wait()
            return self.allowed

        app.state.kiro_agentcore.session_authorizer = authorize
        status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "must not run"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            self.allowed = False
            release.set()
            state = await asyncio.wait_for(manager.wait(task_id), 5)
            self.assertEqual((state.state, state.error_type), ("failed", "PermissionError"))
            self.assertFalse(self.log.exists())
        finally:
            release.set()

    async def test_cancelled_start_does_not_leave_a_running_or_orphaned_task(self):
        entered, release = asyncio.Event(), asyncio.Event()
        app = self.app()
        manager = await self.connect(app)

        async def authorize(req, runtime_id):
            self.calls += 1
            if self.calls == 4:
                entered.set()
                await release.wait()
            return True

        app.state.kiro_agentcore.session_authorizer = authorize
        pending = asyncio.create_task(self.invoke(app, {"action": "background_task_start", "prompt": "must not run"}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertEqual(manager._tasks, {})
            self.assertFalse(self.log.exists())
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_changed_parent_authentication_prevents_worker_connection(self):
        entered, release = asyncio.Event(), asyncio.Event()
        app = self.app()
        manager = await self.connect(app)
        service = app.state.kiro_agentcore

        async def authorize(req, runtime_id):
            self.calls += 1
            if self.calls == 5:
                entered.set()
                await release.wait()
            return True

        service.session_authorizer = authorize
        status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "must not run"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            service.sessions["session-a"].client.options.env["KIRO_API_KEY"] = "changed"
            release.set()
            state = await asyncio.wait_for(manager.wait(task_id), 5)
            self.assertEqual((state.state, state.error_type), ("failed", "PermissionError"))
            self.assertFalse(self.log.exists())
        finally:
            release.set()

    async def test_revocation_after_initial_turn_blocks_the_requested_prompt(self):
        async def revoke_before_requested(event, tool_id, context):
            if event["prompt"] == "requested":
                self.allowed = False
            return {}

        app = self.app(options_factory=lambda workspace, runtime_id: KiroAgentOptions(
            agent="selected", agents={"selected": AgentDefinition("Test", "Test", initial_prompt="initial")},
            hooks={"promptSubmit": [HookMatcher(hooks=[revoke_before_requested])]},
            env={"FAKE_LOG_REQUESTS": str(self.log)}))
        manager = await self.connect(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "requested"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        state = await asyncio.wait_for(manager.wait(task_id), 5)
        self.assertEqual(state.state, "failed")
        self.assertIsNone(manager.retained_result(task_id))
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        prompts = [item["params"]["prompt"][0]["text"] for item in requests
                   if item.get("method") == "session/prompt"]
        self.assertEqual(prompts, ["initial"])

    async def test_capacity_is_bounded_and_factory_must_be_explicit(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "invalid", cli_path=str(FAKE), api_key="synthetic")
        with self.assertRaisesRegex(ValueError, "background_tasks_factory"):
            create_agentcore_app(config, session_authorizer=self.authorize,
                                 background_task_options_factory=lambda workspace, runtime: KiroAgentOptions())
        app = self.app(max_tasks=1)
        manager = await self.connect(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "first"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        await manager.wait(task_id)
        self.assertEqual((await self.invoke(app, {"action": "background_task_start", "prompt": "second"}))[0], 429)
        status, body, _ = await self.invoke(app, {"action": "background_tasks", "include_completed": True})
        self.assertEqual((status, [item["task_id"] for item in json.loads(body)["tasks"]]), (200, [task_id]))
        self.assertEqual((await self.invoke(app, {"action": "background_tasks", "include_completed": "true"}))[0], 400)
        self.assertEqual((await self.invoke(app, {"action": "background_task_forget", "task_id": task_id}))[0], 200)
        self.assertEqual((await self.invoke(app, {"action": "background_task_result", "task_id": task_id}))[0], 404)
        self.assertEqual((await self.invoke(app, {"action": "background_task_start", "prompt": "second"}))[0], 200)

    async def test_running_task_cannot_be_forgotten(self):
        app = self.app()
        manager = await self.connect(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "cancel"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        self.assertEqual((await self.invoke(app, {"action": "background_task_forget", "task_id": task_id}))[0], 409)
        self.assertTrue(manager.owns(task_id))
        self.assertEqual((await self.invoke(app, {"action": "background_task_stop", "task_id": task_id}))[0], 200)

    async def test_stop_during_delayed_spawn_times_out_without_late_prompt(self):
        app = self.app(shutdown_timeout=0.1)
        manager = await self.connect(app)
        entered, release = asyncio.Event(), asyncio.Event()
        spawned = []
        create = asyncio.create_subprocess_exec
        async def delayed_spawn(*args, **kwargs):
            process = await create(*args, **kwargs)
            spawned.append(process)
            entered.set()
            await release.wait()
            return process
        with patch("kiro_agent_sdk.client.asyncio.create_subprocess_exec", side_effect=delayed_spawn):
            try:
                status, body, _ = await self.invoke(app, {
                    "action": "background_task_start", "prompt": "must not be sent"})
                self.assertEqual(status, 200)
                task_id = json.loads(body)["task_id"]
                await asyncio.wait_for(entered.wait(), 5)
                status, _, _ = await self.invoke(app, {"action": "background_task_stop", "task_id": task_id})
                self.assertEqual(status, 504)
                self.assertEqual(manager.status(task_id).state, "cancelling")
                release.set()
                self.assertEqual((await asyncio.wait_for(manager.wait(task_id), 5)).state, "cancelled")
                self.assertIsNotNone(spawned[0].returncode)
                self.assertFalse(self.log.exists())
            finally:
                release.set()

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    async def test_stop_during_blocked_spawn_escalates_without_sending_prompt(self):
        app = self.app(shutdown_timeout=7)
        manager = await self.connect(app)
        entered, release = asyncio.Event(), asyncio.Event()
        spawned = []
        create = asyncio.create_subprocess_exec
        async def delayed_spawn(*args, **kwargs):
            process = await create(
                sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            spawned.append(process)
            await asyncio.wait_for(process.stdout.readline(), 5)
            process.stdin.write(b"x" * (2 * 1024 * 1024))
            entered.set()
            await release.wait()
            return process
        with patch("kiro_agent_sdk.client.asyncio.create_subprocess_exec", side_effect=delayed_spawn):
            pending = None
            try:
                status, body, _ = await self.invoke(app, {
                    "action": "background_task_start", "prompt": "must not be sent"})
                self.assertEqual(status, 200)
                task_id = json.loads(body)["task_id"]
                await asyncio.wait_for(entered.wait(), 5)
                pending = asyncio.create_task(self.invoke(app, {"action": "background_task_stop", "task_id": task_id}))
                async def wait_for_cancellation():
                    while manager.status(task_id).state != "cancelling":
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_cancellation(), 5)
                release.set()
                status, body, _ = await asyncio.wait_for(pending, 10)
                self.assertEqual((status, json.loads(body)["task"]["state"]), (200, "cancelled"))
                self.assertEqual(spawned[0].returncode, -signal.SIGKILL)
                self.assertFalse(self.log.exists())
                self.assertEqual((await manager.wait(task_id)).state, "cancelled")
            finally:
                release.set()
                if spawned and spawned[0].returncode is None:
                    try:
                        os.killpg(spawned[0].pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                if spawned and spawned[0].returncode is None:
                    await asyncio.wait_for(spawned[0].wait(), 5)

    async def test_revocation_after_forget_suppresses_confirmation(self):
        app = self.app()
        manager = await self.connect(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_start", "prompt": "first"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        await manager.wait(task_id)
        forget = manager.forget

        def revoke_after_forget(ident):
            forget(ident)
            self.allowed = False

        with patch.object(manager, "forget", side_effect=revoke_after_forget):
            status, body, _ = await self.invoke(app, {"action": "background_task_forget", "task_id": task_id})
        self.assertEqual(status, 403)
        self.assertNotIn(task_id.encode(), body)
        self.assertFalse(manager.owns(task_id))
