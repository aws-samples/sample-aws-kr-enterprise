"""AgentCoreConfig values reach each session's client, and their precedence over options_factory."""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from test_agentcore import FAKE, HEADER, request

if importlib.util.find_spec("starlette"):
    from kiro_agent_sdk import KiroAgentOptions
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


@unittest.skipUnless(importlib.util.find_spec("starlette"), "AgentCore extra is absent")
class AgentCoreConfigEffectContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.apps = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()

    async def allow(self, req, runtime_id):
        return True

    async def session(self, config, factory=None, prompt="hello"):
        app = create_agentcore_app(config, session_authorizer=self.allow, options_factory=factory)
        self.apps.append(app)
        status, body, _ = await request(app, "POST", "/invocations", json.dumps({"prompt": prompt}).encode(),
                                        [(HEADER, b"session-a")])
        return status, json.loads(body), app.state.kiro_agentcore.sessions.get("session-a")

    def config(self, **values):
        return AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / f"state-{len(self.apps)}", cli_path=str(FAKE), **values)

    async def test_session_state_lives_privately_under_the_configured_directory(self):
        config = self.config(api_key="synthetic")
        status, body, session = await self.session(config)
        self.assertEqual((status, body["response"]), (200, "hello"))
        self.assertTrue(session.directory.resolve().is_relative_to(config.state_dir.resolve()))
        self.assertEqual(stat.S_IMODE(session.directory.stat().st_mode), 0o700)
        self.assertTrue(str(session.client.options.cwd).startswith(str(session.directory)))

    @unittest.skipUnless(os.name == "posix", "requires an executable wrapper script")
    async def test_configured_cli_engine_timeouts_and_key_override_the_options_factory(self):
        marker = self.root / "invoked"
        wrapper = self.root / "kiro-wrapper"
        wrapper.write_text(f"#!/bin/sh\necho invoked >> {marker}\nexec {sys.executable} {FAKE} \"$@\"\n")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "override", cli_path=str(wrapper), api_key="configured-key",
                                 agent_engine="v3", startup_timeout=7, turn_timeout=9)
        factory = lambda workspace, resume: KiroAgentOptions(
            tools=[], cli_path=str(self.root / "never-run"), agent_engine="v2", startup_timeout=99,
            turn_timeout=99, env={"KIRO_API_KEY": "factory-key"})
        status, body, session = await self.session(config, factory)
        self.assertEqual((status, body["response"]), (200, "hello"))
        options = session.client.options
        self.assertEqual((options.agent_engine, options.startup_timeout, options.turn_timeout,
                          options.env["KIRO_API_KEY"]), ("v3", 7, 9, "configured-key"))
        self.assertEqual(marker.read_text().splitlines(), ["invoked"])
        status, body, session = await self.session(self.config(api_key="synthetic", agent_engine="v2"))
        self.assertEqual((status, session.client.options.agent_engine), (200, "v2"))

    async def test_configured_model_applies_only_to_the_default_options(self):
        _, _, session = await self.session(self.config(api_key="synthetic", model="model-one"))
        self.assertEqual(session.client.options.model, "model-one")
        _, _, session = await self.session(self.config(api_key="synthetic", model="model-one"),
                                           lambda workspace, resume: KiroAgentOptions(tools=[], model="model-two"))
        self.assertEqual(session.client.options.model, "model-two")

    async def test_prompt_size_limit_rejects_before_a_session_starts(self):
        status, body, session = await self.session(self.config(api_key="synthetic", max_prompt_bytes=4), prompt="toolong")
        self.assertEqual((status, body["error"]), (413, "Prompt is too large"))
        self.assertIsNone(session)
        status, body, _ = await self.session(self.config(api_key="synthetic", max_prompt_bytes=8), prompt="fits")
        self.assertEqual((status, body["response"]), (200, "fits"))


if __name__ == "__main__":
    unittest.main()
