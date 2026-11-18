import asyncio
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request
from test_background_tasks import ControlledClient

if HAS_STARLETTE:
    from kiro_agent_sdk import (AgentMessage, BackgroundTaskManager, KiroAgentOptions, ResultMessage,
                                TextBlock, UserMessage)
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreBackgroundStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.apps = []
        self.managers = {}
        self.allowed = True
        self.calls = 0
        ControlledClient.instances = []
        ControlledClient.gate = asyncio.Event()
        ControlledClient.disconnect_gate = None
        ControlledClient.event_count = 0
        ControlledClient.fail = ControlledClient.fail_result = ControlledClient.fail_disconnect = False
        self.patch = patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ControlledClient)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, runtime_id):
        self.calls += 1
        return self.allowed and req.headers.get("x-test-identity") == runtime_id

    def app(self, *, max_sessions=4, timeout=5, enabled=True, history_events=8, history_bytes=16384,
            start=False):
        def factory(workspace, runtime_id):
            manager = BackgroundTaskManager(max_history_events=history_events, max_history_bytes=history_bytes,
                                            max_retained_result_bytes=4096)
            self.managers[runtime_id] = manager
            return manager
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / str(len(self.apps)), cli_path=str(FAKE),
            api_key="synthetic", max_sessions=max_sessions, allow_sdk_events=True,
            allow_background_task_events=True, allow_background_task_event_stream=enabled,
            background_task_event_stream_timeout=timeout, allow_background_task_results=start)
        app = create_agentcore_app(config, session_authorizer=self.authorize,
                                  options_factory=lambda workspace, resume: KiroAgentOptions(tools=[]),
                                  background_tasks_factory=factory,
                                  background_task_options_factory=(
                                      lambda workspace, runtime: KiroAgentOptions(
                                          tools=[], include_partial_messages=True)) if start else None)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, runtime_id="session-a"):
        return await request(app, "POST", "/invocations", json.dumps(payload).encode(),
                             [(HEADER, runtime_id.encode()), (b"x-test-identity", runtime_id.encode())], timeout=20)

    async def connect(self, app):
        self.assertEqual((await self.invoke(app, {"prompt": "connect"}))[0], 200)
        return self.managers["session-a"]

    def stream_request(self, app, task_id, *, on_send=None, after=0, disconnect=None):
        incoming = [{"type": "http.request", "body": json.dumps({
            "action": "background_task_events_stream", "task_id": task_id, "after": after}).encode(),
                     "more_body": False}]
        never = asyncio.Event()
        sent = []
        async def receive():
            if incoming:
                return incoming.pop(0)
            if disconnect is not None:
                await disconnect.wait()
                return {"type": "http.disconnect"}
            return await never.wait()
        async def send(message):
            if on_send is not None:
                outcome = on_send(message)
                if inspect.isawaitable(outcome):
                    await outcome
            sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/invocations", "raw_path": b"/invocations", "query_string": b"", "root_path": "",
                 "headers": [(HEADER, b"session-a"), (b"x-test-identity", b"session-a")],
                 "server": ("localhost", 8080), "client": ("localhost", 1)}
        async def run():
            await app(scope, receive, send)
            status = next(item["status"] for item in sent if item["type"] == "http.response.start")
            headers = dict(next(item["headers"] for item in sent if item["type"] == "http.response.start"))
            body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
            return status, headers, body
        return asyncio.create_task(run())

    async def wait_for(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), 5)

    @staticmethod
    def events(body):
        return [json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: ")]

    async def test_stream_sends_existing_and_later_events_then_completion(self):
        produced, release = asyncio.Event(), asyncio.Event()
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("first")])
                produced.set()
                await release.wait()
                yield ResultMessage(self.session_id, 'end_turn', False, result="final")
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app()
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            await asyncio.wait_for(produced.wait(), 5)
            def on_send(message):
                if message["type"] == "http.response.body" and b'"type": "events"' in message.get("body", b""):
                    release.set()
            pending = self.stream_request(app, task_id, on_send=on_send)
            try:
                status, headers, body = await asyncio.wait_for(pending, 5)
                events = self.events(body)
                self.assertEqual(status, 200)
                self.assertIn(b"text/event-stream", headers[b"content-type"])
                self.assertEqual(headers[b"cache-control"], b"no-store")
                pages = [item for item in events if item["type"] == "events"]
                self.assertEqual([event["sequence"] for page in pages for event in page["events"]], [1, 2])
                self.assertEqual(events[-1]["type"], "complete")
                self.assertEqual(events[-1]["task"]["state"], "completed")
                self.assertEqual(app.state.kiro_agentcore.sessions["session-a"].background_event_streams, 0)
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    async def test_remote_start_stream_and_result_with_real_sdk_client_and_fake_cli(self):
        self.patch.stop()
        app = self.app(start=True, timeout=10, history_events=16)
        await self.connect(app)
        status, body, _ = await self.invoke(app, {
            "action": "background_task_start", "request_id": "a" * 32, "prompt": "background hello"})
        self.assertEqual(status, 200)
        task_id = json.loads(body)["task_id"]
        status, _, body = await asyncio.wait_for(self.stream_request(app, task_id), 10)
        self.assertEqual(status, 200)
        messages = self.events(body)
        types = [event["type"] for page in messages if page["type"] == "events" for event in page["events"]]
        self.assertIn("StreamEvent", types)
        self.assertIn("ResultMessage", types)
        self.assertEqual(messages[-1]["type"], "complete")
        status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["result"]), (200, "background hello"))

    async def test_multiple_streams_observe_the_same_later_event(self):
        release = asyncio.Event()
        class EventClient(ControlledClient):
            async def receive_response(self):
                await release.wait()
                yield AgentMessage([TextBlock("shared")])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app()
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            streams = [self.stream_request(app, task_id) for _ in range(2)]
            try:
                session = app.state.kiro_agentcore.sessions["session-a"]
                await self.wait_for(lambda: session.background_event_streams == 2)
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*streams), 5)
                for status, _, body in results:
                    self.assertEqual(status, 200)
                    self.assertIn(b"shared", body)
                    self.assertEqual(self.events(body)[-1]["type"], "complete")
                self.assertEqual(session.background_event_streams, 0)
            finally:
                release.set()
                for stream in streams:
                    if not stream.done():
                        stream.cancel()
                await asyncio.gather(*streams, return_exceptions=True)

    async def test_revocation_after_headers_suppresses_private_event(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("private event")])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app()
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            def on_send(message):
                if message["type"] == "http.response.start":
                    self.allowed = False
            status, _, body = await asyncio.wait_for(self.stream_request(app, task_id, on_send=on_send), 5)
            self.assertEqual(status, 200)
            self.assertNotIn(b"private event", body)
            self.assertEqual(self.events(body)[0]["type"], "error")
            self.assertEqual(app.state.kiro_agentcore.sessions["session-a"].background_event_streams, 0)

    async def test_forgotten_task_after_headers_suppresses_private_event(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("private event")])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app()
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            def on_send(message):
                if message["type"] == "http.response.start":
                    manager.forget(task_id)
            status, _, body = await asyncio.wait_for(self.stream_request(app, task_id, on_send=on_send), 5)
            self.assertEqual(status, 200)
            self.assertNotIn(b"private event", body)
            self.assertEqual(self.events(body)[0]["type"], "error")

    async def test_revocation_after_first_page_hides_later_events(self):
        produced, release = asyncio.Event(), asyncio.Event()
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("permitted")])
                produced.set()
                await release.wait()
                yield AgentMessage([TextBlock("private later")])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app()
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            await asyncio.wait_for(produced.wait(), 5)
            def on_send(message):
                if message["type"] == "http.response.body" and b"permitted" in message.get("body", b""):
                    self.allowed = False
                    release.set()
            try:
                status, _, body = await asyncio.wait_for(
                    self.stream_request(app, task_id, on_send=on_send), 5)
                self.assertEqual(status, 200)
                self.assertIn(b"permitted", body)
                self.assertNotIn(b"private later", body)
                self.assertEqual(self.events(body)[-1]["type"], "error")
            finally:
                release.set()

    async def test_stream_reports_evicted_history_gaps(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                for text in ("first", "second", "third"):
                    yield AgentMessage([TextBlock(text)])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app(history_events=1)
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            status, _, body = await asyncio.wait_for(self.stream_request(app, task_id), 5)
            self.assertEqual(status, 200)
            page = self.events(body)[0]
            self.assertEqual((page["gap"], page["history_dropped"], page["next_cursor"]), (True, 2, 3))
            self.assertEqual(page["events"][0]["message"]["content"][0]["text"], "third")

    async def test_stream_reports_unsupported_events_as_gaps(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield object()
                yield AgentMessage([TextBlock("supported")])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app()
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            status, _, body = await asyncio.wait_for(self.stream_request(app, task_id), 5)
            self.assertEqual(status, 200)
            page = self.events(body)[0]
            self.assertEqual((page["gap"], page["history_dropped"], page["next_cursor"]), (True, 1, 2))
            self.assertEqual(page["events"][0]["message"]["content"][0]["text"], "supported")

    async def test_stream_splits_large_pages_under_response_limits(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                for _ in range(8):
                    yield UserMessage([{"a": "x"} for _ in range(1000)])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app(timeout=20, history_events=16, history_bytes=512 * 1024)
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            status, _, body = await asyncio.wait_for(self.stream_request(app, task_id), 5)
            self.assertEqual(status, 200)
            pages = [event for event in self.events(body) if event["type"] == "events"]
            self.assertGreater(len(pages), 1)
            self.assertEqual([entry["sequence"] for page in pages for entry in page["events"]],
                             list(range(1, 9)))

    async def test_send_failure_releases_stream_pin(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("output")])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app()
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            def on_send(message):
                if message["type"] == "http.response.body":
                    raise OSError("synthetic send failure")
            pending = self.stream_request(app, task_id, on_send=on_send)
            await asyncio.gather(pending, return_exceptions=True)
            self.assertEqual(app.state.kiro_agentcore.sessions["session-a"].background_event_streams, 0)

    async def test_uncooperative_send_timeout_releases_stream_pin(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("output")])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app(timeout=0.1)
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            await manager.wait(task_id)
            async def on_send(message):
                if message["type"] == "http.response.body" and message.get("body"):
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
            pending = self.stream_request(app, task_id, on_send=on_send)
            try:
                await asyncio.wait_for(entered.wait(), 5)
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2)
                self.assertEqual(app.state.kiro_agentcore.sessions["session-a"].background_event_streams, 0)
            finally:
                release.set()
                await asyncio.sleep(0)

    async def test_stream_settings_and_input_are_explicit(self):
        with self.assertRaisesRegex(ValueError, "allow_background_task_events"):
            AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, cli_path=str(FAKE), api_key="synthetic",
                            allow_background_task_event_stream=True)
        for value in (0, -1, 301, True, float("inf"), float("nan")):
            with self.subTest(timeout=value), self.assertRaises(ValueError):
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, cli_path=str(FAKE), api_key="synthetic",
                                background_task_event_stream_timeout=value)
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_SDK_EVENTS": "true",
                                     "KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_EVENTS": "true",
                                     "KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_EVENT_STREAM": "true",
                                     "KIRO_AGENTCORE_BACKGROUND_TASK_EVENT_STREAM_TIMEOUT": "12"}, clear=True):
            config = AgentCoreConfig.from_env()
            self.assertTrue(config.allow_background_task_event_stream)
            self.assertEqual(config.background_task_event_stream_timeout, 12)
        app = self.app(enabled=False)
        manager = await self.connect(app)
        task_id = await manager.start("hello")
        self.assertEqual((await self.invoke(app, {
            "action": "background_task_events_stream", "task_id": task_id}))[0], 403)
        app = self.app()
        manager = await self.connect(app)
        task_id = await manager.start("hello")
        for fields in ({"after": -1}, {"limit": 0}, {"limit": True}, {"extra": "bad"}):
            with self.subTest(fields=fields):
                self.assertEqual((await self.invoke(app, {
                    "action": "background_task_events_stream", "task_id": task_id, **fields}))[0], 400)

    async def test_revocation_while_waiting_for_pool_hides_task_existence(self):
        app = self.app()
        await self.connect(app)
        service = app.state.kiro_agentcore
        await service.pool_lock.acquire()
        self.calls = 0
        pending = asyncio.create_task(self.invoke(app, {
            "action": "background_task_events_stream", "task_id": "sdk-task-" + "a" * 32}))
        try:
            await self.wait_for(lambda: self.calls >= 1)
            self.allowed = False
            service.pool_lock.release()
            status, body, _ = await asyncio.wait_for(pending, 5)
            self.assertEqual(status, 403)
            self.assertNotIn(b"Unknown background task", body)
        finally:
            if service.pool_lock.locked():
                service.pool_lock.release()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_timeout_has_resume_cursor_and_does_not_stop_the_task(self):
        app = self.app(timeout=0.05)
        manager = await self.connect(app)
        task_id = await manager.start("hello")
        status, _, body = await asyncio.wait_for(self.stream_request(app, task_id), 5)
        self.assertEqual(status, 200)
        self.assertEqual(self.events(body)[-1], {"type": "timeout", "next_cursor": 0})
        self.assertEqual(manager.status(task_id).state, "running")
        self.assertEqual(app.state.kiro_agentcore.sessions["session-a"].background_event_streams, 0)

    async def test_cancelled_before_worker_start_finishes_stream(self):
        app = self.app()
        manager = await self.connect(app)
        task_id = await manager.start("hello")
        self.assertEqual((await manager.stop(task_id)).state, "cancelled")
        status, _, body = await asyncio.wait_for(self.stream_request(app, task_id), 5)
        self.assertEqual(status, 200)
        self.assertEqual((self.events(body)[-1]["type"], self.events(body)[-1]["task"]["state"]),
                         ("complete", "cancelled"))

    async def test_timeout_cursor_resumes_without_repeating_events(self):
        produced, release = asyncio.Event(), asyncio.Event()
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("first")])
                produced.set()
                await release.wait()
                yield AgentMessage([TextBlock("second")])
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.app(timeout=1)
            manager = await self.connect(app)
            task_id = await manager.start("hello")
            try:
                await asyncio.wait_for(produced.wait(), 5)
                status, _, body = await asyncio.wait_for(self.stream_request(app, task_id), 5)
                self.assertEqual(status, 200)
                first = self.events(body)
                self.assertEqual([item["type"] for item in first], ["events", "timeout"])
                self.assertEqual(first[-1]["next_cursor"], 1)
                release.set()
                await manager.wait(task_id)
                status, _, body = await asyncio.wait_for(self.stream_request(app, task_id, after=1), 5)
                self.assertEqual(status, 200)
                pages = [item for item in self.events(body) if item["type"] == "events"]
                self.assertEqual([event["sequence"] for page in pages for event in page["events"]], [2])
            finally:
                release.set()

    async def test_stream_capacity_and_cancellation_release_pins(self):
        app = self.app()
        manager = await self.connect(app)
        task_id = await manager.start("hello")
        pending = [self.stream_request(app, task_id) for _ in range(4)]
        try:
            session = app.state.kiro_agentcore.sessions["session-a"]
            await self.wait_for(lambda: session.background_event_streams == 4)
            status, _, _ = await self.invoke(app, {"action": "background_task_events_stream", "task_id": task_id})
            self.assertEqual(status, 429)
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.assertEqual(session.background_event_streams, 0)

    async def test_client_disconnect_releases_stream_pin(self):
        app = self.app()
        manager = await self.connect(app)
        task_id = await manager.start("hello")
        disconnected = asyncio.Event()
        pending = self.stream_request(app, task_id, disconnect=disconnected)
        try:
            session = app.state.kiro_agentcore.sessions["session-a"]
            await self.wait_for(lambda: session.background_event_streams == 1)
            disconnected.set()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 5)
            self.assertEqual(session.background_event_streams, 0)
        finally:
            disconnected.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_shutdown_closes_live_stream_and_releases_pin(self):
        app = self.app()
        manager = await self.connect(app)
        task_id = await manager.start("hello")
        pending = self.stream_request(app, task_id)
        session = app.state.kiro_agentcore.sessions["session-a"]
        try:
            await self.wait_for(lambda: session.background_event_streams == 1)
            await app.state.kiro_agentcore.close()
            _, _, body = await asyncio.wait_for(pending, 5)
            self.assertEqual(self.events(body)[-1]["type"], "error")
            self.assertEqual(session.background_event_streams, 0)
            self.assertEqual(app.state.kiro_agentcore.active, 0)
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_stream_pin_prevents_session_eviction(self):
        app = self.app(max_sessions=1)
        manager = await self.connect(app)
        task_id = await manager.start("hello")
        pending = self.stream_request(app, task_id)
        try:
            session = app.state.kiro_agentcore.sessions["session-a"]
            await self.wait_for(lambda: session.background_event_streams == 1)
            status, _, _ = await self.invoke(app, {"prompt": "other"}, runtime_id="session-b")
            self.assertEqual(status, 429)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        status, _, _ = await self.invoke(app, {"prompt": "other"}, runtime_id="session-b")
        self.assertEqual(status, 200)
        self.assertEqual(manager.status(task_id).state, "cancelled")

    async def test_stream_does_not_block_another_turn_in_the_same_session(self):
        app = self.app()
        manager = await self.connect(app)
        task_id = await manager.start("hello")
        pending = self.stream_request(app, task_id)
        try:
            session = app.state.kiro_agentcore.sessions["session-a"]
            await self.wait_for(lambda: session.background_event_streams == 1)
            status, body, _ = await self.invoke(app, {"prompt": "another turn"})
            self.assertEqual((status, json.loads(body)["response"]), (200, "another turn"))
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
