import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request
from test_background_tasks import ControlledClient

if HAS_STARLETTE:
    from kiro_agent_sdk import (AgentMessage, BackgroundTaskManager, KiroAgentOptions, PermissionResultDeny,
                                ResultMessage, TextBlock, UserMessage)
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreBackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.apps = []
        self.managers = {}
        self.allowed = True
        self.calls = 0
        self.revoke_at = None
        ControlledClient.instances = []
        ControlledClient.gate = asyncio.Event()
        ControlledClient.disconnect_gate = None
        ControlledClient.event_count = 0
        ControlledClient.fail = ControlledClient.fail_result = ControlledClient.fail_disconnect = False
        self.patch = patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ControlledClient)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def asyncTearDown(self):
        if ControlledClient.disconnect_gate is not None:
            ControlledClient.disconnect_gate.set()
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, runtime_id):
        self.calls += 1
        return (self.allowed and self.calls != self.revoke_at and
                req.headers.get("x-test-identity") == runtime_id)

    def make_app(self, *, max_sessions=4, task_factory=None, options_factory=None, shutdown_timeout=1,
                 retained_bytes=0, allow_results=False, history_events=0, history_bytes=0, allow_events=False,
                 retain_structured=False):
        def factory(workspace, runtime_id):
            manager = BackgroundTaskManager(shutdown_timeout=shutdown_timeout,
                                            max_retained_result_bytes=retained_bytes,
                                            max_history_events=history_events, max_history_bytes=history_bytes,
                                            retain_structured_output=retain_structured)
            self.managers[runtime_id] = manager
            return manager

        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / str(len(self.apps)), cli_path=str(FAKE),
                                 api_key="synthetic", max_sessions=max_sessions,
                                 allow_background_task_results=allow_results, allow_sdk_events=allow_events,
                                 allow_background_task_events=allow_events)
        app = create_agentcore_app(config, session_authorizer=self.authorize,
            options_factory=options_factory or (lambda workspace, resume: KiroAgentOptions(tools=[])),
            background_tasks_factory=task_factory or factory)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, *, runtime_id="session-a", identity=None):
        return await request(app, "POST", "/invocations", json.dumps(payload).encode(),
            [(HEADER, runtime_id.encode()), (b"x-test-identity", (identity or runtime_id).encode())], timeout=20)

    async def start(self, app, runtime_id="session-a"):
        status, body, _ = await self.invoke(app, {"prompt": "connect"}, runtime_id=runtime_id)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body).get("is_error", False))
        manager = self.managers[runtime_id]
        task_id = await manager.start("sensitive background prompt")
        await asyncio.sleep(0)
        return manager, task_id

    async def test_factory_requires_an_authorizer_and_explicit_enablement(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, cli_path=str(FAKE), api_key="synthetic")
        with self.assertRaisesRegex(ValueError, "session_authorizer"):
            create_agentcore_app(config, background_tasks_factory=lambda workspace, runtime_id: BackgroundTaskManager())
        with self.assertRaisesRegex(ValueError, "callable"):
            create_agentcore_app(config, session_authorizer=self.authorize, background_tasks_factory=object())
        app = create_agentcore_app(config, session_authorizer=self.authorize)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"action": "background_tasks"}))[0], 403)

    async def test_owner_can_inspect_and_stop_only_own_session_tasks(self):
        app = self.make_app()
        manager_a, task_a = await self.start(app)
        manager_b, task_b = await self.start(app, "session-b")
        status, body, headers = await self.invoke(app, {"action": "background_tasks"})
        listing = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual([task["task_id"] for task in listing["tasks"]], [task_a])
        self.assertFalse(listing["truncated"])
        self.assertNotIn(b"sensitive", body)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual((await self.invoke(app, {"action": "background_tasks"},
                                            runtime_id="session-b", identity="session-a"))[0], 403)
        self.assertEqual((await self.invoke(app, {"action": "background_task_status", "task_id": task_a},
                                            runtime_id="session-b"))[0], 404)
        status, body, _ = await self.invoke(app, {"action": "background_task_stop", "task_id": task_a})
        self.assertEqual((status, json.loads(body)["task"]["state"]), (200, "cancelled"))
        self.assertEqual(manager_a.status(task_a).state, "cancelled")
        self.assertEqual(manager_b.status(task_b).state, "running")
        self.assertEqual((await self.invoke(app, {"action": "background_task_status", "task_id": task_a}))[0], 200)
        self.assertEqual((await self.invoke(app, {"action": "background_task_stop", "task_id": "../bad"}))[0], 400)

    async def test_revoked_owner_cannot_stop_or_observe_task(self):
        app = self.make_app()
        manager, task_id = await self.start(app)
        self.calls = 0
        self.revoke_at = 2
        with patch.object(manager, "stop", wraps=manager.stop) as stop:
            status, body, _ = await self.invoke(app, {"action": "background_task_stop", "task_id": task_id})
        self.assertEqual(status, 403)
        self.assertNotIn(task_id.encode(), body)
        stop.assert_not_called()
        self.assertEqual(manager.status(task_id).state, "running")
        self.revoke_at = None

    async def test_revocation_after_stop_suppresses_the_response(self):
        app = self.make_app()
        manager, task_id = await self.start(app)
        stop = manager.stop

        async def revoke_after_stop(ident):
            status = await stop(ident)
            self.allowed = False
            return status

        with patch.object(manager, "stop", side_effect=revoke_after_stop):
            status, body, _ = await self.invoke(app, {"action": "background_task_stop", "task_id": task_id})
        self.assertEqual(status, 403)
        self.assertNotIn(task_id.encode(), body)
        self.assertEqual(manager.status(task_id).state, "cancelled")

    async def test_revocation_during_listing_suppresses_task_metadata(self):
        app = self.make_app()
        manager, task_id = await self.start(app)
        active_statuses = manager.active_statuses

        def revoke_after_read(*, limit):
            statuses = active_statuses(limit=limit)
            self.allowed = False
            return statuses

        with patch.object(manager, "active_statuses", side_effect=revoke_after_read):
            status, body, _ = await self.invoke(app, {"action": "background_tasks"})
        self.assertEqual(status, 403)
        self.assertNotIn(task_id.encode(), body)
        self.assertEqual(manager.status(task_id).state, "running")

    async def test_reconnect_keeps_the_same_owned_manager(self):
        app = self.make_app()
        manager, task_id = await self.start(app)
        service = app.state.kiro_agentcore
        session = service.sessions["session-a"]
        await service._discard_client(session)
        status, body, _ = await self.invoke(app, {"prompt": "reconnect"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body).get("is_error", False))
        self.assertIs(session.background_tasks, manager)
        self.assertIs(session.client._background_task_manager, manager)
        status, body, _ = await self.invoke(app, {"action": "background_task_status", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["task"]["state"]), (200, "running"))

    async def test_invalid_status_is_not_exposed(self):
        app = self.make_app()
        manager, task_id = await self.start(app)
        status = replace(manager.status(task_id), session_id="bad\nPRIVATE")
        with patch.object(manager, "status", return_value=status):
            code, body, _ = await self.invoke(app, {"action": "background_task_status", "task_id": task_id})
        self.assertEqual(code, 500)
        self.assertNotIn(b"PRIVATE", body)

    async def test_retained_result_requires_opt_in_and_is_owner_scoped(self):
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="private answer")

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            app = self.make_app(retained_bytes=64, allow_results=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            status, body, headers = await self.invoke(
                app, {"action": "background_task_result", "task_id": task_id})
            self.assertEqual((status, json.loads(body)["result"]), (200, "private answer"))
            self.assertFalse(json.loads(body)["is_error"])
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertEqual((await self.invoke(app, {
                "action": "background_task_result", "task_id": task_id}, identity="other"))[0], 403)
            await self.start(app, "session-b")
            status, body, _ = await self.invoke(app, {
                "action": "background_task_result", "task_id": task_id}, runtime_id="session-b")
            self.assertEqual(status, 404)
            self.assertNotIn(b"private answer", body)
            self.assertEqual((await self.invoke(app, {
                "action": "background_task_result", "task_id": task_id}))[0], 200)
            self.assertEqual(len([item async for item in manager.events(task_id)]), 1)

    async def test_retained_result_from_a_real_sdk_client_with_fake_cli(self):
        self.patch.stop()
        app = self.make_app(retained_bytes=256, allow_results=True)
        status, _, _ = await self.invoke(app, {"prompt": "connect"})
        self.assertEqual(status, 200)
        session = app.state.kiro_agentcore.sessions["session-a"]
        manager = self.managers["session-a"]
        task_id = await manager.start("background hello", options=KiroAgentOptions(
            cwd=session.directory / "workspace", cli_path=str(FAKE), agent_engine="v3",
            env={"KIRO_HOME": str(self.root / "background-home")}))
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["result"]), (200, "background hello"))

    async def test_structured_result_is_optional_copied_and_owner_authorized(self):
        output = {"items": [{"name": "private"}]}
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="text", structured_output=output)

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            app = self.make_app(retained_bytes=256, allow_results=True, retain_structured=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            output["items"][0]["name"] = "changed"
            status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
            self.assertEqual((status, json.loads(body)["structured_output"]),
                             (200, {"items": [{"name": "private"}]}))
            self.assertEqual((await self.invoke(app, {
                "action": "background_task_result", "task_id": task_id}, identity="other"))[0], 403)
            manager.retained_result(task_id).structured_output["items"][0]["name"] = "local mutation"
            status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
            self.assertEqual((status, json.loads(body)["structured_output"]),
                             (200, {"items": [{"name": "private"}]}))

    async def test_structured_result_from_a_real_sdk_client_with_fake_cli(self):
        self.patch.stop()
        app = self.make_app(retained_bytes=1024, allow_results=True, retain_structured=True)
        self.assertEqual((await self.invoke(app, {"prompt": "connect"}))[0], 200)
        session = app.state.kiro_agentcore.sessions["session-a"]
        manager = self.managers["session-a"]
        task_id = await manager.start('{"ok": true}', options=KiroAgentOptions(
            cwd=session.directory / "workspace", cli_path=str(FAKE), agent_engine="v3",
            output_format={"type": "json_schema", "schema": {
                "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}},
            env={"KIRO_HOME": str(self.root / "structured-home")}))
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
        self.assertEqual((status, json.loads(body)["structured_output"]), (200, {"ok": True}))

    async def test_structured_retention_change_during_result_read_fails_closed(self):
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="text", structured_output={"secret": "private"})

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            app = self.make_app(retained_bytes=256, allow_results=True, retain_structured=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            read = manager.retained_result
            def change_setting(ident):
                result = read(ident)
                manager._retain_structured_output = False
                return result
            with patch.object(manager, "retained_result", side_effect=change_setting):
                status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
            self.assertEqual(status, 403)
            self.assertNotIn(b"private", body)

    async def test_structured_result_is_not_retained_without_separate_opt_in(self):
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="text", structured_output={"secret": "private"})
        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            app = self.make_app(retained_bytes=256, allow_results=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
            self.assertEqual(status, 200)
            self.assertNotIn("structured_output", json.loads(body))
            self.assertNotIn(b"private", body)

    async def test_result_requires_server_and_manager_opt_ins(self):
        app = self.make_app(retained_bytes=64)
        manager, task_id = await self.start(app)
        ControlledClient.gate.set()
        await manager.wait(task_id)
        self.assertEqual((await self.invoke(app, {"action": "background_task_result", "task_id": task_id}))[0], 403)
        app = self.make_app(allow_results=True)
        manager, task_id = await self.start(app)
        await manager.wait(task_id)
        self.assertEqual((await self.invoke(app, {"action": "background_task_result", "task_id": task_id}))[0], 409)
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "invalid", cli_path=str(FAKE), api_key="synthetic",
                                 allow_background_task_results=True)
        with self.assertRaisesRegex(ValueError, "background_tasks_factory"):
            create_agentcore_app(config, session_authorizer=self.authorize)

    async def test_result_opt_in_validation_and_environment(self):
        with self.assertRaisesRegex(ValueError, "allow_background_task_results"):
            AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, cli_path=str(FAKE), api_key="synthetic",
                            allow_background_task_results="true")
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic",
                                     "KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_RESULTS": "true"}, clear=True):
            self.assertTrue(AgentCoreConfig.from_env().allow_background_task_results)

    async def test_revocation_during_result_read_never_exposes_content(self):
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="private answer")

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            app = self.make_app(retained_bytes=64, allow_results=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            retained_result = manager.retained_result

            def revoke_after_read(ident):
                result = retained_result(ident)
                self.allowed = False
                return result

            with patch.object(manager, "retained_result", side_effect=revoke_after_read):
                status, body, _ = await self.invoke(
                    app, {"action": "background_task_result", "task_id": task_id})
            self.assertEqual(status, 403)
            self.assertNotIn(b"private answer", body)

    async def test_result_must_still_belong_to_the_session_after_authorization(self):
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="private answer")

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            app = self.make_app(retained_bytes=64, allow_results=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            entered, release = asyncio.Event(), asyncio.Event()
            calls = 0

            async def authorizer(req, runtime_id):
                nonlocal calls
                calls += 1
                if calls == 3:
                    entered.set()
                    await release.wait()
                return True

            app.state.kiro_agentcore.session_authorizer = authorizer
            pending = asyncio.create_task(self.invoke(app, {"action": "background_task_result", "task_id": task_id}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                manager.forget(task_id)
                release.set()
                status, body, _ = await pending
                self.assertEqual(status, 409)
                self.assertNotIn(b"private answer", body)
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    async def test_unfinished_and_oversized_results_are_not_exposed(self):
        app = self.make_app(retained_bytes=3, allow_results=True)
        manager, task_id = await self.start(app)
        self.assertEqual((await self.invoke(app, {"action": "background_task_result", "task_id": task_id}))[0], 409)
        ControlledClient.gate.set()
        await manager.wait(task_id)
        self.assertEqual((await self.invoke(app, {"action": "background_task_result", "task_id": task_id}))[0], 404)

        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="secret")

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            app = self.make_app(retained_bytes=3, allow_results=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
            self.assertEqual(status, 413)
            self.assertNotIn(b"secret", body)

    async def test_result_json_response_is_bounded_after_escaping(self):
        class ResultClient(ControlledClient):
            async def receive_response(self):
                yield ResultMessage(self.session_id, 'end_turn', False, result="\x01" * 200000)

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", ResultClient):
            app = self.make_app(retained_bytes=1024 * 1024, allow_results=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            status, body, _ = await self.invoke(app, {"action": "background_task_result", "task_id": task_id})
            self.assertEqual(status, 413)
            self.assertLess(len(body), 1000)

    async def test_event_history_is_owner_scoped_and_nonconsuming(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("private first")])
                yield AgentMessage([TextBlock("private second")])
                yield ResultMessage(self.session_id, 'end_turn', False, result="private final")

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.make_app(history_events=2, history_bytes=8192, allow_events=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            payload = {"action": "background_task_events", "task_id": task_id, "after": 0, "limit": 1}
            status, body, headers = await self.invoke(app, payload)
            page = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual((page["next_cursor"], page["latest_sequence"], page["history_dropped"],
                              page["gap"], page["has_more"]), (2, 3, 1, True, True))
            self.assertEqual(page["events"][0]["message"]["content"][0]["text"], "private second")
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertNotIn(b"content-length", headers)
            status, body, _ = await self.invoke(app, {**payload, "after": 2})
            self.assertEqual((status, json.loads(body)["events"][0]["message"]["result"]), (200, "private final"))
            self.assertEqual((await self.invoke(app, payload, identity="other"))[0], 403)
            self.assertEqual(len([item async for item in manager.events(task_id)]), 3)
            self.assertEqual((await self.invoke(app, payload))[0], 200)

    async def test_event_history_requires_both_server_and_manager_opt_ins(self):
        with self.assertRaisesRegex(ValueError, "allow_sdk_events"):
            AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, cli_path=str(FAKE), api_key="synthetic",
                            allow_background_task_events=True)
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "invalid", cli_path=str(FAKE), api_key="synthetic",
                                 allow_sdk_events=True, allow_background_task_events=True)
        with self.assertRaisesRegex(ValueError, "background_tasks_factory"):
            create_agentcore_app(config, session_authorizer=self.authorize)
        with patch.dict(os.environ, {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_SDK_EVENTS": "true",
                                     "KIRO_AGENTCORE_ALLOW_BACKGROUND_TASK_EVENTS": "true"}, clear=True):
            self.assertTrue(AgentCoreConfig.from_env().allow_background_task_events)
        with self.assertRaisesRegex(ValueError, "allow_background_task_events"):
            AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, cli_path=str(FAKE), api_key="synthetic",
                            allow_sdk_events=True, allow_background_task_events="true")
        app = self.make_app(history_events=2, history_bytes=1024)
        manager, task_id = await self.start(app)
        self.assertEqual((await self.invoke(app, {"action": "background_task_events", "task_id": task_id}))[0], 403)
        app = self.make_app(allow_events=True)
        manager, task_id = await self.start(app)
        self.assertEqual((await self.invoke(app, {"action": "background_task_events", "task_id": task_id}))[0], 409)
        for fields in ({"after": -1}, {"after": True}, {"limit": 0}, {"limit": 65}, {"extra": "bad"}):
            with self.subTest(fields=fields):
                self.assertEqual((await self.invoke(app, {
                    "action": "background_task_events", "task_id": task_id, **fields}))[0], 400)

    async def test_event_history_can_be_polled_while_task_is_running(self):
        sent, release = asyncio.Event(), asyncio.Event()
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("first")])
                sent.set()
                await release.wait()
                yield ResultMessage(self.session_id, 'end_turn', False, result="final")

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.make_app(history_events=4, history_bytes=8192, allow_events=True)
            manager, task_id = await self.start(app)
            try:
                await asyncio.wait_for(sent.wait(), 5)
                status, body, _ = await self.invoke(app, {"action": "background_task_events", "task_id": task_id})
                page = json.loads(body)
                self.assertEqual((status, page["task"]["state"], page["next_cursor"]), (200, "running", 1))
                self.assertEqual(page["events"][0]["message"]["content"][0]["text"], "first")
                self.assertEqual((await self.invoke(app, {
                    "action": "background_task_events", "task_id": task_id, "after": 2}))[0], 400)
                release.set()
                await manager.wait(task_id)
                status, body, _ = await self.invoke(app, {
                    "action": "background_task_events", "task_id": task_id, "after": page["next_cursor"]})
                self.assertEqual((status, json.loads(body)["events"][0]["message"]["result"]), (200, "final"))
            finally:
                release.set()

    async def test_event_history_from_real_sdk_client_with_fake_cli(self):
        self.patch.stop()
        app = self.make_app(history_events=16, history_bytes=8192, allow_events=True)
        self.assertEqual((await self.invoke(app, {"prompt": "connect"}))[0], 200)
        session = app.state.kiro_agentcore.sessions["session-a"]
        manager = self.managers["session-a"]
        task_id = await manager.start("background hello", options=KiroAgentOptions(
            cwd=session.directory / "workspace", cli_path=str(FAKE), agent_engine="v3",
            include_partial_messages=True, env={"KIRO_HOME": str(self.root / "background-home")}))
        self.assertEqual((await manager.wait(task_id)).state, "completed")
        status, body, _ = await self.invoke(app, {"action": "background_task_events", "task_id": task_id})
        page = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("StreamEvent", [item["type"] for item in page["events"]])
        self.assertIn("ResultMessage", [item["type"] for item in page["events"]])
        self.assertFalse(page["gap"])

    async def test_event_page_over_the_structural_limit_can_be_retried_in_smaller_pages(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                for _ in range(8):
                    yield UserMessage([{"a": "x"} for _ in range(1000)])

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.make_app(history_events=16, history_bytes=512 * 1024, allow_events=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            status, body, _ = await self.invoke(app, {"action": "background_task_events", "task_id": task_id})
            self.assertEqual(status, 413)
            self.assertLess(len(body), 1000)
            status, body, _ = await self.invoke(app, {
                "action": "background_task_events", "task_id": task_id, "limit": 1})
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(body)["events"]), 1)
            self.assertTrue(json.loads(body)["has_more"])

    async def test_revocation_during_event_snapshot_suppresses_content(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("private event")])

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.make_app(history_events=2, history_bytes=2048, allow_events=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            history = manager.event_history
            def revoke_after_read(ident, *, after, limit):
                page = history(ident, after=after, limit=limit)
                self.allowed = False
                return page
            with patch.object(manager, "event_history", side_effect=revoke_after_read):
                status, body, _ = await self.invoke(app, {"action": "background_task_events", "task_id": task_id})
            self.assertEqual(status, 403)
            self.assertNotIn(b"private event", body)

    async def test_result_and_event_output_are_reauthorized_after_headers(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("private event")])
                yield ResultMessage(self.session_id, 'end_turn', False, result="private result", structured_output={"secret": "private structured"})

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.make_app(history_events=4, history_bytes=8192, allow_events=True,
                                retained_bytes=1024, allow_results=True, retain_structured=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            for action, secret in (("background_task_events", b"private event"),
                                   ("background_task_result", b"private result")):
                with self.subTest(action=action):
                    self.allowed = True
                    sent = []
                    incoming = [{"type": "http.request", "body": json.dumps(
                        {"action": action, "task_id": task_id}).encode(), "more_body": False}]
                    never = asyncio.Event()
                    async def receive():
                        return incoming.pop(0) if incoming else await never.wait()
                    async def send(message):
                        sent.append(message)
                        if message["type"] == "http.response.start":
                            self.allowed = False
                    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                             "http_version": "1.1", "method": "POST", "scheme": "http",
                             "path": "/invocations", "raw_path": b"/invocations", "query_string": b"",
                             "root_path": "", "headers": [(HEADER, b"session-a"),
                                                          (b"x-test-identity", b"session-a")],
                             "server": ("localhost", 8080), "client": ("localhost", 1)}
                    await asyncio.wait_for(app(scope, receive, send), 5)
                    body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
                    self.assertNotIn(secret, body)
                    self.assertNotIn(b"private structured", body)
                    self.assertTrue(json.loads(body)["is_error"])

    async def test_forgetting_task_after_headers_suppresses_event_body(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("private event")])

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.make_app(history_events=2, history_bytes=2048, allow_events=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            sent = []
            incoming = [{"type": "http.request", "body": json.dumps({
                "action": "background_task_events", "task_id": task_id}).encode(), "more_body": False}]
            never = asyncio.Event()
            async def receive():
                return incoming.pop(0) if incoming else await never.wait()
            async def send(message):
                sent.append(message)
                if message["type"] == "http.response.start":
                    manager.forget(task_id)
            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                     "http_version": "1.1", "method": "POST", "scheme": "http",
                     "path": "/invocations", "raw_path": b"/invocations", "query_string": b"",
                     "root_path": "", "headers": [(HEADER, b"session-a"),
                                                  (b"x-test-identity", b"session-a")],
                     "server": ("localhost", 8080), "client": ("localhost", 1)}
            await asyncio.wait_for(app(scope, receive, send), 5)
            body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
            self.assertNotIn(b"private event", body)
            self.assertTrue(json.loads(body)["is_error"])

    async def test_history_limit_change_during_snapshot_prevents_exposure(self):
        class EventClient(ControlledClient):
            async def receive_response(self):
                yield AgentMessage([TextBlock("private event")])

        with patch("kiro_agent_sdk.background_tasks.KiroSDKClient", EventClient):
            app = self.make_app(history_events=2, history_bytes=2048, allow_events=True)
            manager, task_id = await self.start(app)
            await manager.wait(task_id)
            history = manager.event_history
            def change_limit(ident, *, after, limit):
                page = history(ident, after=after, limit=limit)
                manager._max_history_bytes = 1024
                return page
            with patch.object(manager, "event_history", side_effect=change_limit):
                status, body, _ = await self.invoke(app, {"action": "background_task_events", "task_id": task_id})
            self.assertEqual(status, 403)
            self.assertNotIn(b"private event", body)

    async def test_replaced_manager_during_authorization_cannot_be_observed(self):
        app = self.make_app()
        manager, task_id = await self.start(app)
        entered = asyncio.Event()
        release = asyncio.Event()
        original_authorizer = self.authorize
        calls = 0

        async def delayed_authorizer(req, runtime_id):
            nonlocal calls
            calls += 1
            if calls > 1:
                entered.set()
                await release.wait()
            return await original_authorizer(req, runtime_id)

        service = app.state.kiro_agentcore
        service.session_authorizer = delayed_authorizer
        pending = asyncio.create_task(self.invoke(app, {"action": "background_tasks"}))
        other = BackgroundTaskManager()
        try:
            await asyncio.wait_for(entered.wait(), 5)
            service.sessions["session-a"].client.attach_background_tasks(other)
            release.set()
            status, body, _ = await pending
            self.assertEqual(status, 403)
            self.assertNotIn(task_id.encode(), body)
            self.assertEqual(manager.status(task_id).state, "running")
        finally:
            release.set()
            await other.aclose()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_revocation_during_session_start_closes_new_manager(self):
        app = self.make_app()
        self.calls = 0
        self.revoke_at = 2
        status, body, _ = await self.invoke(app, {"prompt": "connect"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        manager = self.managers["session-a"]
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await manager.start("later")
        self.assertIsNone(app.state.kiro_agentcore.sessions["session-a"].background_tasks)
        self.revoke_at = None

    async def test_factory_cannot_share_a_manager_between_sessions(self):
        shared = BackgroundTaskManager()
        app = self.make_app(task_factory=lambda workspace, runtime_id: shared)
        status, body, _ = await self.invoke(app, {"prompt": "connect"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body).get("is_error", False))
        status, body, _ = await self.invoke(app, {"prompt": "other"}, runtime_id="session-b")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertIs(app.state.kiro_agentcore.sessions["session-a"].background_tasks, shared)
        self.assertIsNone(app.state.kiro_agentcore.sessions["session-b"].background_tasks)

    async def test_eviction_closes_and_cancels_owned_manager(self):
        app = self.make_app(max_sessions=1)
        manager, task_id = await self.start(app)
        status, body, _ = await self.invoke(app, {"prompt": "other"}, runtime_id="session-b")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body).get("is_error", False))
        self.assertEqual(manager.status(task_id).state, "cancelled")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await manager.start("later")
        self.assertNotIn("session-a", app.state.kiro_agentcore.sessions)

    async def test_shutdown_reports_unfinished_background_cleanup_and_can_be_retried(self):
        ControlledClient.disconnect_gate = asyncio.Event()
        app = self.make_app(shutdown_timeout=0.01)
        manager, task_id = await self.start(app)
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            await app.state.kiro_agentcore.close()
        self.assertEqual(manager.status(task_id).state, "cancelling")
        self.assertIn("session-a", app.state.kiro_agentcore.sessions)
        ControlledClient.disconnect_gate.set()
        await asyncio.sleep(0)
        await app.state.kiro_agentcore.close()
        self.assertEqual(manager.status(task_id).state, "cancelled")

    async def test_shutdown_retains_failed_manager_until_its_cleanup_succeeds(self):
        app = self.make_app()
        manager, task_id = await self.start(app)
        ControlledClient.fail_disconnect = True
        ControlledClient.gate.set()
        try:
            self.assertTrue((await manager.wait(task_id)).cleanup_pending)
            service = app.state.kiro_agentcore
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await service.close()
            self.assertIs(service.sessions["session-a"].background_tasks, manager)
            self.assertTrue(manager.owns(task_id))
            self.assertTrue(manager.status(task_id).cleanup_pending)
            ControlledClient.fail_disconnect = False
            await service.close()
            self.assertEqual(service.sessions, {})
            self.assertFalse(manager.status(task_id).cleanup_pending)
            self.assertTrue(ControlledClient.instances[-1].disconnected)
        finally:
            ControlledClient.fail_disconnect = False

    async def test_shutdown_rejects_new_sessions_and_controls_until_cleanup_finishes(self):
        ControlledClient.disconnect_gate = asyncio.Event()
        app = self.make_app()
        manager, task_id = await self.start(app)
        closing = asyncio.create_task(app.state.kiro_agentcore.close())
        try:
            async def wait_for_close():
                while not manager._closed:
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_for_close(), 5)
            self.assertEqual((await self.invoke(app, {"prompt": "must not start"}, runtime_id="session-b"))[0], 503)
            self.assertEqual((await self.invoke(app, {"action": "background_task_status", "task_id": task_id}))[0], 503)
            self.assertNotIn("session-b", self.managers)
        finally:
            ControlledClient.disconnect_gate.set()
            await asyncio.wait_for(closing, 5)
        self.assertEqual(manager.status(task_id).state, "cancelled")

    async def test_shutdown_during_authorization_does_not_create_a_manager(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_authorizer(req, runtime_id):
            entered.set()
            await release.wait()
            return True

        app = self.make_app()
        app.state.kiro_agentcore.session_authorizer = delayed_authorizer
        pending = asyncio.create_task(self.invoke(app, {"prompt": "must not start"}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await app.state.kiro_agentcore.close()
            release.set()
            status, _, _ = await pending
            self.assertEqual(status, 403)
            self.assertEqual(self.managers, {})
            self.assertEqual(app.state.kiro_agentcore.sessions, {})
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_shutdown_during_eviction_does_not_create_a_new_session(self):
        ControlledClient.disconnect_gate = asyncio.Event()
        app = self.make_app(max_sessions=1)
        manager, task_id = await self.start(app)
        pending = asyncio.create_task(self.invoke(app, {"prompt": "other"}, runtime_id="session-b"))
        closing = None
        try:
            async def wait_for_eviction():
                while not manager._closed:
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_for_eviction(), 5)
            closing = asyncio.create_task(app.state.kiro_agentcore.close())
            await asyncio.sleep(0)
            ControlledClient.disconnect_gate.set()
            await asyncio.wait_for(closing, 5)
            status, _, _ = await asyncio.wait_for(pending, 5)
            self.assertIn(status, (403, 409, 503))
            self.assertNotIn("session-b", self.managers)
            self.assertEqual(manager.status(task_id).state, "cancelled")
        finally:
            ControlledClient.disconnect_gate.set()
            for task in (pending, closing):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_stop_timeout_reports_pending_cleanup_without_claiming_success(self):
        ControlledClient.disconnect_gate = asyncio.Event()
        app = self.make_app(shutdown_timeout=0.01)
        manager, task_id = await self.start(app)
        status, body, _ = await self.invoke(app, {"action": "background_task_stop", "task_id": task_id})
        self.assertEqual(status, 504)
        self.assertNotIn(task_id.encode(), body)
        self.assertEqual(manager.status(task_id).state, "cancelling")
        ControlledClient.disconnect_gate.set()
        self.assertEqual((await manager.wait(task_id)).state, "cancelled")

    async def test_stop_works_while_parent_turn_waits_for_permission(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def deny(name, inputs, context):
            started.set()
            await release.wait()
            return PermissionResultDeny()

        app = self.make_app(options_factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], can_use_tool=deny))
        manager, task_id = await self.start(app)
        pending = asyncio.create_task(self.invoke(app, {"prompt": "permission"}))
        try:
            await asyncio.wait_for(started.wait(), 5)
            status, body, _ = await self.invoke(app, {"action": "background_task_stop", "task_id": task_id})
            self.assertEqual((status, json.loads(body)["task"]["state"]), (200, "cancelled"))
            self.assertEqual(manager.status(task_id).state, "cancelled")
        finally:
            release.set()
            await asyncio.wait_for(pending, 10)

