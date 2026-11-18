"""Malformed AgentCore action requests are rejected with 400 and without side effects."""
import json
import tempfile
import unittest
from pathlib import Path

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request

if HAS_STARLETTE:
    from kiro_agent_sdk import (BackgroundTaskManager, KiroAgentOptions, SQLiteBackgroundRequestStore,
                                SQLiteBackgroundResultStore)
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app

TASK = "sdk-task-" + "a" * 32
BAD_TASK = "../bad"

# Actions whose exact field set is validated before any session lease.
STRICT = [
    ("code_status", {"extra": True}), ("code_init", {"extra": True}), ("code_overview", {"extra": True}),
    ("compact", {"extra": True}), ("compact", {"include_events": "yes"}),
    ("mcp_reconnect", {"server_name": "probe"}), ("mcp_reconnect", {"server_name": "probe", "start_oauth": 1}),
    ("mcp_reconnect", {"server_name": "", "start_oauth": False}),
    ("mcp_revoke_oauth", {}), ("mcp_revoke_oauth", {"server_name": ""}),
    ("mcp_revoke_oauth", {"server_name": "probe", "extra": True}),
    ]

BACKGROUND = [
    "background_tasks", "background_task_status", "background_task_stop", "background_task_result",
    "background_task_start", "background_task_retry", "background_task_forget", "background_task_events",
    "background_task_events_stream", "background_task_archive", "background_task_archive_get",
    "background_task_archive_delete", "background_task_archive_events", "background_task_archive_events_get",
    "background_task_archive_events_delete", "background_task_checkpoint_events",
    "background_task_checkpoint_events_get", "background_task_checkpoint_events_delete",
]

INTROSPECTION = [
    "server_info", "context_usage", "mcp_status", "sdk_permission_mode", "available_commands"
]


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreActionInputContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.apps, self.created, self.managers = [], [], {}

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()

    async def authorize(self, req, runtime_id):
        return req.headers.get("x-test-identity") == runtime_id

    def options(self, workspace, resume):
        self.created.append(workspace)
        return KiroAgentOptions(tools=[])

    def app(self, **fields):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / str(len(self.apps)), cli_path=str(FAKE), **fields)
        app = create_agentcore_app(config, options_factory=self.options, session_authorizer=self.authorize)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, runtime_id="session-a"):
        status, body, _ = await request(app, "POST", "/invocations", json.dumps(payload).encode(),
                                        [(HEADER, runtime_id.encode()), (b"x-test-identity", runtime_id.encode())])
        return status, json.loads(body) if body else None

    async def test_strict_controls_reject_malformed_fields_before_any_session_exists(self):
        app = self.app(api_key="synthetic", agent_engine="v3")
        for action, fields in STRICT:
            with self.subTest(action=action, fields=fields):
                status, body = await self.invoke(app, {"action": action, **fields})
                self.assertEqual(status, 400)
                self.assertIn("error", body)
        self.assertEqual(app.state.kiro_agentcore.sessions, {})
        self.assertEqual(self.created, [])

    async def test_enabled_background_actions_reject_malformed_requests_without_tasks_or_records(self):
        result_store = SQLiteBackgroundResultStore(self.root / "results.sqlite")
        request_store = SQLiteBackgroundRequestStore(self.root / "requests.sqlite")

        def managers(workspace, runtime_id):
            manager = BackgroundTaskManager(max_retained_result_bytes=4096, max_history_events=8,
                                            max_history_bytes=16384)
            self.managers[runtime_id] = manager
            return manager
        config = AgentCoreConfig(allow_unauthorized_sessions=True,
            state_dir=self.root / "background", cli_path=str(FAKE), api_key="synthetic",
            allow_background_task_results=True, allow_sdk_events=True, allow_background_task_events=True,
            allow_background_task_event_stream=True, archive_background_task_events=True,
            allow_background_event_checkpoints=True, allow_background_task_retry=True)
        app = create_agentcore_app(
            config, options_factory=self.options, session_authorizer=self.authorize,
            background_tasks_factory=managers,
            background_task_options_factory=lambda workspace, runtime_id: KiroAgentOptions(tools=[]),
            background_request_store=request_store, background_result_store=result_store)
        self.apps.append(app)
        self.assertEqual((await self.invoke(app, {"prompt": "connect"}))[0], 200)
        manager = self.managers["session-a"]
        for action in BACKGROUND:
            for fields in ({"task_id": TASK, "extra": True}, {"task_id": BAD_TASK}, {"task_id": 1}):
                if action == "background_tasks":
                    fields = {"extra": True}
                elif action in ("background_task_start", "background_task_retry"):
                    fields = {"prompt": "hello", "request_id": "invalid", **({"extra": True} if "extra" in fields else {})}
                with self.subTest(action=action, fields=fields):
                    status, body = await self.invoke(app, {"action": action, **fields})
                    self.assertEqual(status, 400)
                    self.assertIn("error", body)
        self.assertEqual(manager.statuses(limit=10), ([], False))
        self.assertFalse(await request_store.exists("session-a", "a" * 32))
        self.assertIsNone(await result_store.load("session-a", TASK))

    async def test_enabled_introspection_rejects_unknown_fields(self):
        app = self.app(api_key="synthetic", agent_engine="v3", allow_session_introspection=True,
                       allow_command_introspection=True)
        self.assertEqual((await self.invoke(app, {"prompt": "connect"}))[0], 200)
        for action in INTROSPECTION:
            with self.subTest(action=action):
                status, body = await self.invoke(app, {"action": action, "extra": "value"})
                self.assertEqual((status, body["error"]), (400, "Invalid introspection request"))
        self.assertEqual(len(self.created), 1)

    async def test_enabled_interactions_reject_unknown_fields_and_invalid_ids(self):
        app = self.app(api_key="synthetic", allow_interactions=True)
        for payload in ({"action": "interactions", "extra": True}, {"action": "respond"},
                        {"action": "respond", "interaction_id": "../bad", "decision": "allow"},
                        {"action": "respond", "interaction_id": 1, "decision": "allow"}):
            with self.subTest(payload=payload):
                self.assertEqual((await self.invoke(app, payload))[0], 400)
        self.assertEqual(app.state.kiro_agentcore.sessions, {})
        self.assertEqual(self.created, [])

    async def test_enabled_device_actions_reject_unknown_fields_before_a_session(self):
        app = self.app(auth_mode="device", startup_timeout=10)
        for action in ("auth_start", "auth_status", "auth_cancel", "auth_logout"):
            with self.subTest(action=action):
                status, body = await self.invoke(app, {"action": action, "extra": True})
                self.assertEqual((status, body["error"]), (400, "Invalid authentication request"))
        self.assertEqual(app.state.kiro_agentcore.sessions, {})
        self.assertEqual(self.created, [])


if __name__ == "__main__":
    unittest.main()
