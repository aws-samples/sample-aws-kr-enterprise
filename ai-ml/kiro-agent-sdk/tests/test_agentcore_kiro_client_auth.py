import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import KiroAgentOptions
from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request
from test_kiro_client_auth import SECRET, token

if HAS_STARLETTE:
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


@unittest.skipUnless(HAS_STARLETTE, "requires AgentCore")
class AgentCoreKiroClientAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log, self.status = self.root / "requests.jsonl", self.root / "auth-status.jsonl"
        self.apps = []
        self.allowed = True
        self.provider_calls = []
        self.revoke_in_provider = False
        self.identity_metadata = {}

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, session):
        return self.allowed and req.headers.get("x-test-identity") == "owner" and session == "session-a"

    async def authorize_idle(self, session):
        return self.allowed and session == "session-a"

    def token_factory(self, workspace, session):
        async def provider():
            self.provider_calls.append((workspace, session))
            if self.revoke_in_provider:
                self.allowed = False
            return {**token(), **self.identity_metadata}
        return provider

    def app(self, *, factory=True, auth_mode="cli", provider_in_options=False, idle=False,
            idle_authorizer=True):
        def options(workspace, resume):
            async def direct():
                return token()
            return KiroAgentOptions(
                tools=[], kiro_auth_method="client", kiro_access_token_provider=direct if provider_in_options else None,
                kiro_client_auth_idle_mode="authorized" if idle else "reject",
                env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_CLIENT_AUTH_INIT": "1",
                     "FAKE_CLIENT_AUTH_TURN": "1", "FAKE_EXPECT_TOKEN": SECRET,
                     "FAKE_AUTH_STATUS": str(self.status), "FAKE_LOG_REQUESTS": str(self.log),
                     "FAKE_AUTH_STDERR_SECRET": SECRET, **({"FAKE_CLIENT_AUTH_IDLE": "1"} if idle else {}),
                     **({"FAKE_EXPECT_AUTH_METHOD": self.identity_metadata["authMethod"],
                         "FAKE_EXPECT_AUTH_PROVIDER": self.identity_metadata["provider"]}
                        if self.identity_metadata else {})})
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / f"state-{len(self.apps)}", cli_path=str(FAKE),
                                 auth_mode=auth_mode, api_key="synthetic" if auth_mode == "api-key" else None,
                                 agent_engine="v3")
        app = create_agentcore_app(config, options_factory=options, session_authorizer=self.authorize,
                                   kiro_token_provider_factory=self.token_factory if factory else None,
                                   kiro_idle_token_authorizer=(self.authorize_idle
                                                               if factory and idle and idle_authorizer else None))
        self.apps.append(app)
        return app

    async def invoke(self, app, identity="owner", session="session-a", prompt="hello"):
        return await request(app, "POST", "/invocations", json.dumps({"prompt": prompt}).encode(),
                             [(HEADER, session.encode()), (b"x-test-identity", identity.encode())], timeout=20)

    async def test_owner_uses_bound_provider_and_secrets_do_not_escape(self):
        app = self.app()
        self.assertEqual((await self.invoke(app, "other"))[0], 403)
        self.assertEqual((await self.invoke(app, session="session-b"))[0], 403)
        status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        self.assertEqual([session for _, session in self.provider_calls], ["session-a", "session-a"])
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(SECRET, self.log.read_text())
        self.assertNotIn(SECRET, self.status.read_text())

    async def test_owner_identity_metadata_reaches_kiro(self):
        self.identity_metadata = {"authMethod": "external_idp", "provider": "ExternalIdp"}
        app = self.app()
        self.assertEqual((await self.invoke(app, "other"))[0], 403)
        status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        self.assertEqual([session for _, session in self.provider_calls], ["session-a", "session-a"])
        statuses = list(map(json.loads, self.status.read_text().splitlines()))
        self.assertEqual(len(statuses), 2)
        self.assertTrue(all(item["valid"] for item in statuses))
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(SECRET, self.log.read_text())
        self.assertNotIn(SECRET, self.status.read_text())

    async def test_identity_metadata_is_reauthorized_for_idle_refresh(self):
        self.identity_metadata = {"authMethod": "IdC", "provider": "Enterprise"}
        app = self.app(idle=True)
        status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        async def wait():
            while len(self.status.read_text().splitlines()) < 3:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), 3)
        statuses = list(map(json.loads, self.status.read_text().splitlines()))
        self.assertTrue(all(item["valid"] for item in statuses))
        self.assertEqual([session for _, session in self.provider_calls], ["session-a"] * 3)
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn(SECRET, self.log.read_text())

    async def test_requires_owner_and_per_session_factory(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "bad", auth_mode="cli")
        with self.assertRaises(ValueError):
            create_agentcore_app(config, kiro_token_provider_factory=self.token_factory)
        for changes in ({"factory": False}, {"auth_mode": "api-key"}, {"provider_in_options": True}):
            app = self.app(**changes)
            status, body, _ = await self.invoke(app)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["is_error"])
        self.assertEqual(self.provider_calls, [])

    async def test_revoked_owner_after_provider_cannot_send_token(self):
        app = self.app()
        self.revoke_in_provider = True
        status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(SECRET.encode(), body)
        self.assertEqual(len(self.provider_calls), 1)
        statuses = list(map(json.loads, self.status.read_text().splitlines()))
        self.assertFalse(statuses[0]["valid"])
        self.assertNotIn(SECRET, self.log.read_text())

    async def test_provider_factory_change_prevents_token_delivery(self):
        app = self.app()
        service = app.state.kiro_agentcore
        async def changed_provider():
            self.provider_calls.append(("created", "session-a"))
            service.kiro_token_provider_factory = lambda workspace, session: changed_provider
            return token()
        service.kiro_token_provider_factory = lambda workspace, session: changed_provider
        status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(SECRET.encode(), body)
        self.assertFalse(json.loads(self.status.read_text().splitlines()[0])["valid"])

    async def test_idle_refresh_requires_separate_authorizer_and_does_not_capture_request(self):
        from starlette.requests import Request
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "bad-idle", auth_mode="cli")
        with self.assertRaises(ValueError):
            create_agentcore_app(config, session_authorizer=self.authorize,
                                 kiro_idle_token_authorizer=self.authorize_idle)
        missing = self.app(idle=True, idle_authorizer=False)
        status, body, _ = await self.invoke(missing)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        app = self.app(idle=True)
        status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        client = app.state.kiro_agentcore.sessions["session-a"].client
        guard = client.options.kiro_idle_token_authorizer
        self.assertFalse(any(isinstance(cell.cell_contents, Request) for cell in guard.__closure__ or ()))
        async def wait():
            while len(self.status.read_text().splitlines()) < 3:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), 3)
        self.assertTrue(all(item["valid"] for item in map(json.loads, self.status.read_text().splitlines())))
        self.assertEqual([session for _, session in self.provider_calls], ["session-a"] * 3)
        self.assertNotIn(SECRET, self.log.read_text())

    async def test_idle_owner_revocation_blocks_provider(self):
        app = self.app(idle=True)
        status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        self.allowed = False
        async def wait():
            while len(self.status.read_text().splitlines()) < 3:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), 3)
        self.assertEqual(len(self.provider_calls), 2)
        self.assertFalse(json.loads(self.status.read_text().splitlines()[-1])["valid"])

    async def test_revocation_during_idle_provider_discards_token(self):
        entered, release = asyncio.Event(), asyncio.Event()
        app = self.app(idle=True)
        service = app.state.kiro_agentcore
        def factory(workspace, session):
            async def provider():
                self.provider_calls.append((workspace, session))
                if len(self.provider_calls) == 3:
                    entered.set()
                    await release.wait()
                return token()
            return provider
        service.kiro_token_provider_factory = factory
        try:
            status, body, _ = await self.invoke(app)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)["is_error"])
            await asyncio.wait_for(entered.wait(), 3)
            self.allowed = False
            release.set()
            async def wait():
                while len(self.status.read_text().splitlines()) < 3:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), 3)
            self.assertFalse(json.loads(self.status.read_text().splitlines()[-1])["valid"])
            self.assertNotIn(SECRET, self.log.read_text())
        finally:
            release.set()

    async def test_replaced_idle_authorizer_blocks_provider(self):
        app = self.app(idle=True)
        status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        async def replacement(session):
            return True
        app.state.kiro_agentcore.kiro_idle_token_authorizer = replacement
        async def wait():
            while len(self.status.read_text().splitlines()) < 3:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait(), 3)
        self.assertEqual(len(self.provider_calls), 2)
        self.assertFalse(json.loads(self.status.read_text().splitlines()[-1])["valid"])

    async def test_cancel_ignoring_idle_provider_quarantines_session(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        app = self.app(idle=True)
        service = app.state.kiro_agentcore
        def factory(workspace, session):
            async def provider():
                self.provider_calls.append((workspace, session))
                if len(self.provider_calls) == 3:
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                    finished.set()
                return token()
            return provider
        service.kiro_token_provider_factory = factory
        try:
            await self.invoke(app)
            await asyncio.wait_for(entered.wait(), 3)
            session = service.sessions["session-a"]
            client = session.client
            with self.assertRaisesRegex(RuntimeError, "Kiro cleanup failed"):
                await service._discard_client(session)
            self.assertIs(session.client, client)
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            await service._discard_client(session)
            self.assertIsNone(session.client)
            self.assertNotIn(SECRET, self.log.read_text())
        finally:
            release.set()

    async def test_factory_base_exception_is_sanitized(self):
        app = self.app()
        service = app.state.kiro_agentcore
        def failing_factory(workspace, session):
            raise SystemExit(SECRET)
        service.kiro_token_provider_factory = failing_factory
        status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(SECRET.encode(), body)

    async def test_revocation_while_provider_waits_prevents_token_delivery(self):
        entered, release = asyncio.Event(), asyncio.Event()
        app = self.app()
        service = app.state.kiro_agentcore
        def factory(workspace, session):
            async def provider():
                entered.set()
                await release.wait()
                return token()
            return provider
        service.kiro_token_provider_factory = factory
        pending = asyncio.create_task(self.invoke(app))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.allowed = False
            release.set()
            status, body, _ = await asyncio.wait_for(pending, 5)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["is_error"])
            self.assertNotIn(SECRET.encode(), body)
            self.assertFalse(json.loads(self.status.read_text().splitlines()[0])["valid"])
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
