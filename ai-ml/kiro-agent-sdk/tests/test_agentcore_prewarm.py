import json
import tempfile
import unittest
from pathlib import Path

from test_agentcore import FAKE, HAS_STARLETTE, HEADER, request

if HAS_STARLETTE:
    from kiro_agent_sdk import AgentDefinition, KiroAgentOptions
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCorePrewarmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", cli_path=str(FAKE),
                                      api_key="synthetic-test-key", startup_timeout=10, turn_timeout=10)
        self.apps = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()

    def app(self, factory=None, config=None, **kwargs):
        factory = factory or (lambda workspace, resume: KiroAgentOptions(
            tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)}))
        app = create_agentcore_app(config or self.config, options_factory=factory, **kwargs)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, runtime_id=b"session-a", headers=()):
        status, body, _ = await request(app, "POST", "/invocations", json.dumps(payload).encode(),
                                        [(HEADER, runtime_id), *headers])
        return status, json.loads(body)

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def methods(self, name):
        return [item for item in self.requests() if item.get("method") == name]

    async def test_prewarm_starts_the_session_without_a_prompt_and_the_next_prompt_reuses_it(self):
        app = self.app()
        status, body = await self.invoke(app, {"action": "prewarm"})
        self.assertEqual((status, body), (200, {"action": "prewarm", "status": "ready", "already_warm": False,
                                                 "is_error": False}))
        self.assertEqual(len(self.methods("session/new")), 1)
        self.assertEqual(self.methods("session/prompt"), [])
        client = app.state.kiro_agentcore.sessions["session-a"].client
        self.assertTrue(client.session_id)
        status, body = await self.invoke(app, {"prompt": "hello"})
        self.assertEqual((status, body["response"]), (200, "hello"))
        self.assertIs(app.state.kiro_agentcore.sessions["session-a"].client, client)
        self.assertEqual(len(self.methods("session/new")), 1)
        self.assertEqual(len(self.methods("session/prompt")), 1)

    async def test_prewarm_is_idempotent_and_reports_an_already_warm_session(self):
        app = self.app()
        await self.invoke(app, {"action": "prewarm"})
        status, body = await self.invoke(app, {"action": "prewarm"})
        self.assertEqual((status, body["status"], body["already_warm"]), (200, "ready", True))
        await self.invoke(app, {"prompt": "hello"})
        status, body = await self.invoke(app, {"action": "prewarm"})
        self.assertEqual((status, body["already_warm"]), (200, True))
        self.assertEqual(len(self.methods("session/new")), 1)
        self.assertEqual(len(self.methods("session/prompt")), 1)

    async def test_invalid_prewarm_requests_are_rejected_before_any_process(self):
        app = self.app()
        for payload in ({"action": "prewarm", "prompt": "x"}, {"action": "prewarm", "extra": True}):
            with self.subTest(payload=payload):
                status, body = await self.invoke(app, payload)
                self.assertEqual(status, 400)
        self.assertEqual(self.requests(), [])
        self.assertNotIn("session-a", app.state.kiro_agentcore.sessions)

    async def test_unauthorized_prewarm_starts_nothing_and_revoked_authorization_discards_the_client(self):
        allowed = {"value": True}

        async def authorizer(req, session_id):
            if req.headers.get("x-test-identity") != "owner":
                return False
            return allowed["value"]

        app = self.app(session_authorizer=authorizer)
        status, _ = await self.invoke(app, {"action": "prewarm"}, headers=[(b"x-test-identity", b"other")])
        self.assertEqual(status, 403)
        self.assertEqual(self.requests(), [])
        owner = [(b"x-test-identity", b"owner")]
        service = app.state.kiro_agentcore
        original = service._client

        async def revoke_during_connect(*args, **kwargs):
            result = await original(*args, **kwargs)
            allowed["value"] = False
            return result
        service._client = revoke_during_connect
        status, body = await self.invoke(app, {"action": "prewarm"}, headers=owner)
        service._client = original
        self.assertEqual((status, body), (403, {"error": "Session access denied"}))
        self.assertIsNone(service.sessions["session-a"].client)
        allowed["value"] = True
        status, body = await self.invoke(app, {"action": "prewarm"}, headers=owner)
        self.assertEqual((status, body["already_warm"]), (200, False))

    async def test_busy_and_capacity_limits_apply_to_prewarm(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", cli_path=str(FAKE), api_key="synthetic-test-key",
                                 startup_timeout=10, turn_timeout=10, max_sessions=1)
        app = self.app(config=config)
        service = app.state.kiro_agentcore
        lease, _ = await service._lease("session-a")
        try:
            status, body = await self.invoke(app, {"action": "prewarm"})
            self.assertEqual((status, body), (409, {"error": "Session is busy"}))
            status, body = await self.invoke(app, {"action": "prewarm"}, runtime_id=b"session-b")
            self.assertEqual((status, body), (429, {"error": "Session capacity reached"}))
        finally:
            await lease.release()
        status, body = await self.invoke(app, {"action": "prewarm"}, runtime_id=b"session-b")
        self.assertEqual((status, body["status"]), (200, "ready"))

    async def test_failed_prewarm_leaves_no_client_and_a_later_prompt_starts_fresh(self):
        calls = {"count": 0}

        def factory(workspace, resume):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("application options are temporarily unavailable")
            return KiroAgentOptions(tools=[], env={"FAKE_LOG_REQUESTS": str(self.log)})

        app = self.app(factory=factory)
        status, body = await self.invoke(app, {"action": "prewarm"})
        self.assertEqual((status, body), (200, {"error": "Session unavailable", "is_error": True}))
        self.assertIsNone(app.state.kiro_agentcore.sessions["session-a"].client)
        status, body = await self.invoke(app, {"prompt": "hello"})
        self.assertEqual((status, body["response"]), (200, "hello"))

    async def test_prewarm_runs_a_configured_initial_prompt_and_the_next_prompt_drains_it(self):
        async def authorizer(req, session_id):
            return req.headers.get("x-test-identity") == "owner"

        def factory(workspace, resume):
            return KiroAgentOptions(agent="selected", env={"FAKE_LOG_REQUESTS": str(self.log)},
                                    agents={"selected": AgentDefinition("Selected", "Follow the task",
                                                                        initial_prompt="initial")})
        app = self.app(factory=factory, session_authorizer=authorizer)
        owner = [(b"x-test-identity", b"owner")]
        status, body = await self.invoke(app, {"action": "prewarm"}, headers=owner)
        self.assertEqual((status, body["status"]), (200, "ready"))
        status, body = await self.invoke(app, {"prompt": "hello"}, headers=owner)
        self.assertEqual((status, body["response"]), (200, "hello"))
        prompts = [item["params"]["prompt"][0]["text"] for item in self.methods("session/prompt")]
        self.assertEqual(prompts, ["initial", "hello"])
        self.assertIsNone(app.state.kiro_agentcore.sessions["session-a"].prewarm_guard)

    async def test_closing_the_service_stops_a_prewarmed_client(self):
        app = self.app()
        await self.invoke(app, {"action": "prewarm"})
        client = app.state.kiro_agentcore.sessions["session-a"].client
        await app.state.kiro_agentcore.close()
        self.assertFalse(client._connected)
        status, body = await self.invoke(app, {"action": "prewarm"})
        self.assertEqual(status, 503)


if __name__ == "__main__":
    unittest.main()
