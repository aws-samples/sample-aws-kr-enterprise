import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request

if HAS_STARLETTE:
    from kiro_agent_sdk import HookMatcher, KiroAgentOptions, PromptBlockedError
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app

OWNER = [(b"x-test-identity", b"owner")]


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCorePromptBlockDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", cli_path=str(FAKE),
                                      api_key="synthetic", startup_timeout=10, turn_timeout=10,
                                      allow_prompt_block_details=True)
        self.apps = []
        self.output = {"block": "PRIVATE_REASON", "warning": "PRIVATE_WARNING"}

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, session):
        return req.headers.get("x-test-identity") == "owner" and session == "session-a"

    def app(self, config=None, authorizer=True, hook=None, stop=False, options=None):
        async def default_hook(event, tool_id, context):
            return self.output
        def factory(workspace, resume):
            values = {"tools": [], "env": {"FAKE_LOG_REQUESTS": str(self.log), "FAKE_PROMPT_CAPABILITIES": "1"},
                      "hooks": {"promptSubmit": [HookMatcher(hooks=[hook or default_hook])]}}
            if options:
                values.update(options)
            return KiroAgentOptions(**values)
        app = create_agentcore_app(config or self.config, options_factory=factory,
                                   session_authorizer=self.authorize if authorizer else None)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, identity=OWNER, session=b"session-a"):
        return await request(app, "POST", "/invocations", json.dumps(payload).encode(),
                             [(HEADER, session), *identity])

    def details(self, body, streaming):
        if streaming:
            events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith(b"data: ")]
            self.assertEqual(events[-1]["type"], "error")
            return events[-1]["prompt_block"]
        parsed = json.loads(body)
        self.assertTrue(parsed["is_error"])
        return parsed["prompt_block"]

    async def test_opt_in_is_required_for_default_and_unauthorized_requests(self):
        disabled = self.app(replace(self.config, allow_prompt_block_details=False))
        requested = {"prompt": "PRIVATE_PROMPT", "include_prompt_block_details": True}
        self.assertEqual((await self.invoke(disabled, requested))[0], 403)
        no_authorizer = self.app(replace(self.config, state_dir=self.root / "no-auth"), authorizer=False)
        self.assertEqual((await self.invoke(no_authorizer, requested))[0], 403)
        app = self.app(replace(self.config, state_dir=self.root / "enabled"))
        self.assertEqual((await self.invoke(app, requested, [(b"x-test-identity", b"other")]))[0], 403)
        self.assertEqual((await self.invoke(app, requested, session=b"session-b"))[0], 403)
        self.assertEqual((await self.invoke(app, {**requested, "include_prompt_block_details": 1}))[0], 400)
        self.assertFalse(self.log.exists())
        for stream in (False, True):
            status, body, _ = await self.invoke(app, {"prompt": "PRIVATE_PROMPT", "stream": stream})
            self.assertEqual(status, 200)
            self.assertIn(b"Agent invocation failed", body)
            self.assertNotIn(b"PRIVATE_", body)
        self.assertNotIn("PRIVATE_PROMPT", self.log.read_text())

    async def test_authorized_details_are_opt_in_and_never_sent_to_kiro(self):
        app = self.app()
        for stream in (False, True):
            status, body, headers = await self.invoke(app, {"prompt": "PRIVATE_PROMPT", "stream": stream,
                                                            "include_prompt_block_details": True})
            self.assertEqual(status, 200)
            self.assertEqual(headers[b"cache-control"], b"no-store")
            details = self.details(body, stream)
            self.assertEqual(details, {"kind": "blocked", "reasons": ["PRIVATE_REASON"],
                "warnings": ["PRIVATE_WARNING"]})
        self.assertNotIn("PRIVATE_PROMPT", self.log.read_text())
        self.assertNotIn("PRIVATE_REASON", self.log.read_text())

    async def test_details_never_include_the_original_prompt(self):
        app = self.app()
        _, body, _ = await self.invoke(app, {"prompt": "PRIVATE_PROMPT", "include_prompt_block_details": True})
        self.assertNotIn("original_prompt", self.details(body, False))
        self.assertNotIn(b"PRIVATE_PROMPT", body)

    async def test_policy_change_during_hook_suppresses_details(self):
        for change, stream in (("config", False), ("authorizer", False),
                               ("config", True), ("authorizer", True)):
            with self.subTest(change=change, stream=stream):
                started, release = asyncio.Event(), asyncio.Event()
                async def hook(event, tool_id, context):
                    started.set()
                    await release.wait()
                    return self.output
                app = self.app(replace(self.config, state_dir=self.root / (change + str(stream))), hook=hook)
                service = app.state.kiro_agentcore
                pending = asyncio.create_task(self.invoke(app, {"prompt": "PRIVATE_PROMPT",
                    "include_prompt_block_details": True, "stream": stream}))
                await asyncio.wait_for(started.wait(), 5)
                if change == "config":
                    service.config = replace(service.config, allow_prompt_block_details=False)
                else:
                    service.session_authorizer = self.authorize
                release.set()
                status, body, _ = await pending
                self.assertEqual(status, 200)
                if stream:
                    self.assertIn(b"Agent invocation failed", body)
                else:
                    self.assertEqual(json.loads(body), {"error": "Agent invocation failed", "is_error": True})
                self.assertNotIn(b"PRIVATE_", body)

    async def test_authorizer_change_while_waiting_for_lease_denies_the_request(self):
        app = self.app()
        service = app.state.kiro_agentcore
        lease, error = await service._lease("session-a")
        self.assertIsNone(error)
        await lease.release()
        async with service.pool_lock:
            pending = asyncio.create_task(self.invoke(app, {"prompt": "PRIVATE_PROMPT",
                "include_prompt_block_details": True}))
            async def wait_for_pool_waiter():
                while not service.pool_lock._waiters:
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_for_pool_waiter(), 5)
            service.session_authorizer = self.authorize
            service.pool_lock.release()
            try:
                status, body, _ = await pending
            finally:
                await service.pool_lock.acquire()
        self.assertEqual(status, 403)
        self.assertNotIn(b"PRIVATE_", body)
        self.assertFalse(self.log.exists())

    async def test_malformed_hook_exception_never_reveals_details(self):
        async def hook(event, tool_id, context):
            error = PromptBlockedError(["SECRET"], event["session_id"])
            error.reasons = ("SECRET" * 20000,)
            raise error
        app = self.app(hook=hook)
        _, body, _ = await self.invoke(app, {"prompt": "PRIVATE_PROMPT", "include_prompt_block_details": True})
        self.assertEqual(json.loads(body), {"error": "Agent invocation failed", "is_error": True})
        self.assertNotIn(b"SECRET", body)

    async def test_other_session_block_never_exposes_details(self):
        async def hook(event, tool_id, context):
            raise PromptBlockedError(["SERVER_SECRET"], "another-session", original_prompt="USER_PROMPT")
        app = self.app(hook=hook)
        _, body, _ = await self.invoke(app, {"prompt": "USER_PROMPT", "include_prompt_block_details": True})
        self.assertEqual(json.loads(body), {"error": "Agent invocation failed", "is_error": True})
        self.assertNotIn(b"SERVER_SECRET", body)
        self.assertNotIn(b"USER_PROMPT", body)

    async def test_policy_change_during_cleanup_does_not_expose_details(self):
        app = self.app()
        service = app.state.kiro_agentcore
        original_discard = service._discard_client
        async def discard_then_revoke(session):
            await original_discard(session)
            service.config = replace(service.config, allow_prompt_block_details=False)
        with patch.object(service, "_discard_client", discard_then_revoke):
            _, body, _ = await self.invoke(app, {"prompt": "PRIVATE_PROMPT", "include_prompt_block_details": True})
        self.assertEqual(json.loads(body), {"error": "Agent invocation failed", "is_error": True})
        self.assertNotIn(b"PRIVATE_", body)

    async def test_boolean_configuration_is_strict(self):
        with self.assertRaises(ValueError):
            replace(self.config, allow_prompt_block_details="true")
        with patch.dict("os.environ", {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_PROMPT_BLOCK_DETAILS": "true"}, clear=True):
            self.assertTrue(AgentCoreConfig.from_env().allow_prompt_block_details)
        with patch.dict("os.environ", {"KIRO_API_KEY": "synthetic", "KIRO_AGENTCORE_ALLOW_PROMPT_BLOCK_DETAILS": "1"}, clear=True):
            with self.assertRaises(ValueError):
                AgentCoreConfig.from_env()
