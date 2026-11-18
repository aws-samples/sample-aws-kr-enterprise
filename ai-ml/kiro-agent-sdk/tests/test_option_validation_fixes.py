"""Regression tests for option values that were previously accepted although invalid."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import CLINotFoundError, InMemorySessionStore, KiroAgentOptions, KiroSDKClient

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


async def callback(*args):
    return None


class _NoStore:
    pass


class OptionValidationFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.log = self.root / "requests.jsonl"

    def options(self, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3", env={"FAKE_LOG_REQUESTS": str(self.log)})
        values.update(changes)
        return KiroAgentOptions(**values)

    def test_invalid_values_are_rejected_at_construction_before_any_process(self):
        store = InMemorySessionStore()
        cases = {
            "add_dirs": ("/data", {"/data": 1}, b"/data", 1, ["\0bad"], [""], [1]),
            "include_thoughts": (1, "false", object(), None),
            "include_partial_messages": (1, "false", object(), None),
            "continue_conversation": (1, "false", object(), None),
            "enable_kiro_hooks": (1, "false", object(), None),
            "require_sandbox": (1, "false", object(), None),
            "fork_session": (1, "yes", object(), None),
            "stderr": ("stderr", 1),
            "can_use_tool": ("allow", 1),
            "session_store": (object(), "store", _NoStore()),
            "agent": ("", "bad\0name", "bad\nname", -1, True, "x" * 300),
            "agent_engine": ("v4", "", "V3", 3, True),
            "output_format": ("json", ["json_schema"], 1),
            "cli_path": ("", "kiro\0cli", 1)
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(option=name, value=repr(value)[:30]), self.assertRaises((ValueError, TypeError)):
                    KiroSDKClient(self.options(**{name: value}))
        resume_cases = {"resume": ("", "bad\0id", "bad\nid", -1, True, "x" * 600)}
        for value in resume_cases["resume"]:
            with self.subTest(option="resume", value=repr(value)[:30]), self.assertRaises((ValueError, TypeError)):
                KiroSDKClient(self.options(resume=value))
        for value in ("bad\0id", "bad\nid", 1, True, "x" * 600):
            with self.subTest(option="resume_session_at", value=repr(value)[:30]), \
                    self.assertRaises((ValueError, TypeError)):
                KiroSDKClient(self.options(resume="old", fork_session=True, resume_session_at=value))
        for value in ("relative/source", 1, str(self.root / "bad\0dir")):
            with self.subTest(option="session_store_source_directory", value=repr(value)[:30]), \
                    self.assertRaises((ValueError, TypeError)):
                KiroSDKClient(self.options(session_store=store, resume="old", resume_from_store=True,
                                           session_store_source_directory=value))
        for value in ("team\nother", "team\0other"):
            with self.subTest(option="reported_credit_scope", value=repr(value)[:30]), \
                    self.assertRaises((ValueError, TypeError)):
                from kiro_agent_sdk import SQLiteCreditLedger
                KiroSDKClient(self.options(max_reported_credits=1, reported_credit_scope=value,
                                           reported_credit_ledger=SQLiteCreditLedger(str(self.root / "c.db"))))
        for value in ("env", {"A": 1}, {1: "b"}, {"A\0": "b"}, {"A": "b\0"}, {"": "b"}, {"A=B": "c"}):
            with self.subTest(option="env", value=repr(value)[:30]), self.assertRaises((ValueError, TypeError)):
                KiroSDKClient(self.options(env=value))
        self.assertFalse(self.log.exists())

    async def test_directory_cli_path_is_reported_as_missing_cli(self):
        client = KiroSDKClient(self.options(cli_path=str(self.root)))
        try:
            with self.assertRaises(CLINotFoundError):
                await client.connect()
        finally:
            await client.disconnect()
        self.assertFalse(self.log.exists())


class OptionChangeGuardFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.log = self.root / "requests.jsonl"

    def prompts(self):
        if not self.log.exists():
            return 0
        return sum(1 for line in self.log.read_text().splitlines() if json.loads(line).get("method") == "session/prompt")

    async def test_mirror_error_policy_change_is_rejected_before_the_prompt(self):
        options = KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3", session_store=InMemorySessionStore(),
                                   session_store_error_policy="continue", env={"FAKE_LOG_REQUESTS": str(self.log)})
        async with KiroSDKClient(options) as client:
            options.session_store_error_policy = "fail"
            with self.assertRaisesRegex(ValueError, "new client"):
                await client.query("must not run")
            self.assertEqual(self.prompts(), 0)
            options.session_store_error_policy = "continue"
            await client.query("hello")
            self.assertEqual([m async for m in client.receive_response()][-1].result, "hello")

    async def test_native_hook_changes_are_rejected_instead_of_ignored(self):
        from kiro_agent_sdk import HookMatcher
        options = KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3", native_hook_bridge_mode="same-user",
                                   native_hooks={"postToolUse": [HookMatcher(hooks=[callback])]},
                                   env={"FAKE_LOG_REQUESTS": str(self.log)})
        async with KiroSDKClient(options) as client:
            options.native_hooks = {}
            with self.assertRaisesRegex(ValueError, "new client"):
                await client.query("must not run")
            self.assertEqual(self.prompts(), 0)

    async def test_permission_mode_option_change_requires_set_permission_mode(self):
        options = KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3", can_use_tool=callback,
                                   env={"FAKE_LOG_REQUESTS": str(self.log)})
        async with KiroSDKClient(options) as client:
            options.permission_mode = "denyUnlisted"
            with self.assertRaisesRegex(ValueError, "set_permission_mode"):
                await client.query("must not run")
            self.assertEqual(self.prompts(), 0)
            options.permission_mode = "ask"
            await client.set_permission_mode("denyUnlisted")
            self.assertEqual(client.sdk_permission_mode, "denyUnlisted")

    async def test_runtime_path_change_names_the_runtime_path(self):
        client = KiroSDKClient(KiroAgentOptions(cwd=self.root, cli_path=FAKE, agent_engine="v3"))
        client.options.native_hook_runtime_path = "/other/acp-server.js"
        with self.assertRaisesRegex(ValueError, "native_hook_runtime_path"):
            client._validate_unsupported_limits()


if __name__ == "__main__":
    unittest.main()


class ResistantRequestCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_resistant_permission_callback_blocks_reconnect_until_it_finishes(self):
        from test_inbound_cancellation import PermissionTransport
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def resistant(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            from kiro_agent_sdk import PermissionResultAllow
            return PermissionResultAllow()
        client = KiroSDKClient(KiroAgentOptions(can_use_tool=resistant), transport=PermissionTransport())
        try:
            await client.connect()
            await client.query("first")
            await asyncio.wait_for(entered.wait(), 5)
            from kiro_agent_sdk import CLIConnectionError
            with self.assertRaisesRegex(CLIConnectionError, "Request callback cleanup is incomplete"):
                await client.disconnect()
            with self.assertRaisesRegex(CLIConnectionError, "Request callback cleanup is incomplete"):
                await client.connect()
            release.set()
            await asyncio.wait_for(finished.wait(), 5)
            await asyncio.sleep(0)
            await client.connect()
        finally:
            release.set()
            await client.disconnect()


class EnvironmentInheritanceTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, **values):
        from unittest import mock
        with mock.patch.dict(os.environ, {"TEST_KIRO_ENV": "inherited-secret"}):
            async with KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine="v3", **values)) as client:
                await client.query("env_probe")
                return [m async for m in client.receive_response()][-1].result

    async def test_minimal_mode_blanks_inherited_values_but_keeps_explicit_env(self):
        self.assertEqual(await self.probe(), "inherited-secret")
        self.assertNotEqual(await self.probe(env_inheritance_mode="minimal"), "inherited-secret")
        self.assertEqual(await self.probe(env_inheritance_mode="minimal", env={"TEST_KIRO_ENV": "explicit"}), "explicit")
        with self.assertRaisesRegex(ValueError, "env_inheritance_mode"):
            KiroSDKClient(KiroAgentOptions(env_inheritance_mode="none"))

    def test_minimal_mode_keeps_the_allowlisted_variables(self):
        from unittest import mock
        from kiro_agent_sdk.process_env import blank_non_allowlisted_environment
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin", "LC_ALL": "C", "AWS_SECRET_ACCESS_KEY": "x"}):
            blanked = blank_non_allowlisted_environment()
        self.assertEqual(blanked["AWS_SECRET_ACCESS_KEY"], "")
        self.assertNotIn("PATH", blanked)
        self.assertNotIn("LC_ALL", blanked)

    async def test_agentcore_sessions_use_the_minimal_environment_by_default(self):
        import importlib.util
        if importlib.util.find_spec("starlette") is None:
            self.skipTest("AgentCore extra is absent")
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        from test_agentcore import FAKE as AGENT_FAKE, HEADER, request
        with tempfile.TemporaryDirectory() as directory:
            for mode, expected in (("minimal", "minimal"), ("inherit", "inherit")):
                config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory) / mode, cli_path=str(AGENT_FAKE), api_key="synthetic",
                                         child_env_mode=mode)

                async def allow(req, runtime_id):
                    return True
                app = create_agentcore_app(config, session_authorizer=allow)
                try:
                    status, _, _ = await request(app, "POST", "/invocations", json.dumps({"prompt": "hello"}).encode(),
                                                 [(HEADER, b"session-a")])
                    self.assertEqual(status, 200)
                    self.assertEqual(app.state.kiro_agentcore.sessions["session-a"].client.options.env_inheritance_mode,
                                     expected)
                finally:
                    await app.state.kiro_agentcore.close()
            with self.assertRaisesRegex(ValueError, "child_env_mode"):
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory) / "x", api_key="synthetic", child_env_mode="none")


class AgentCoreConfigValidationFixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import importlib.util
        if importlib.util.find_spec("starlette") is None:
            self.skipTest("AgentCore extra is absent")

    def test_model_and_secret_key_reject_control_characters_with_accurate_messages(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig
        with tempfile.TemporaryDirectory() as directory:
            for value in ("m\0x", "m\nx", "x" * 300, " ", 1):
                with self.subTest(model=repr(value)[:20]), self.assertRaisesRegex(ValueError, "model must be"):
                    AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), api_key="synthetic", model=value)
            for value in ("key\0name", "key\nname", " ", 1):
                with self.subTest(secret_json_key=repr(value)[:20]), self.assertRaisesRegex(ValueError, "secret_json_key must be"):
                    AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), secret_id="secret-ref", secret_json_key=value)
            with self.assertRaisesRegex(ValueError, "secret_json_key requires secret_id"):
                AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), api_key="synthetic", secret_json_key="key")
            self.assertEqual(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), api_key="synthetic", model="model-one").model,
                             "model-one")

    async def test_configured_model_is_the_default_for_factory_options(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        from test_agentcore import FAKE as AGENT_FAKE, HEADER, request

        async def allow(req, runtime_id):
            return True
        with tempfile.TemporaryDirectory() as directory:
            for factory_model, expected in ((None, "model-one"), ("model-two", "model-two")):
                config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory) / str(factory_model), cli_path=str(AGENT_FAKE),
                                         api_key="synthetic", model="model-one")
                app = create_agentcore_app(config, session_authorizer=allow,
                                           options_factory=lambda workspace, resume, model=factory_model:
                                           KiroAgentOptions(tools=[], model=model))
                try:
                    status, _, _ = await request(app, "POST", "/invocations", json.dumps({"prompt": "hi"}).encode(),
                                                 [(HEADER, b"session-a")])
                    self.assertEqual(status, 200)
                    self.assertEqual(app.state.kiro_agentcore.sessions["session-a"].client.options.model, expected)
                finally:
                    await app.state.kiro_agentcore.close()

    async def test_configured_child_environment_mode_overrides_the_factory(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        from test_agentcore import FAKE as AGENT_FAKE, HEADER, request

        async def allow(req, runtime_id):
            return True
        with tempfile.TemporaryDirectory() as directory:
            for config_mode, factory_mode, expected in (("minimal", "inherit", "minimal"), ("inherit", "minimal", "minimal"),
                                                        ("inherit", "inherit", "inherit")):
                config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory) / (config_mode + factory_mode), cli_path=str(AGENT_FAKE),
                                         api_key="synthetic", child_env_mode=config_mode)
                app = create_agentcore_app(config, session_authorizer=allow,
                                           options_factory=lambda workspace, resume, mode=factory_mode:
                                           KiroAgentOptions(tools=[], env_inheritance_mode=mode))
                try:
                    status, _, _ = await request(app, "POST", "/invocations", json.dumps({"prompt": "hi"}).encode(),
                                                 [(HEADER, b"session-a")])
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        app.state.kiro_agentcore.sessions["session-a"].client.options.env_inheritance_mode, expected)
                finally:
                    await app.state.kiro_agentcore.close()
