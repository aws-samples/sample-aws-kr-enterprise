import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agentcore import HEADER, request

HAS_AGENTCORE = importlib.util.find_spec("starlette") is not None and os.name == "posix"
if HAS_AGENTCORE:
    from kiro_agent_sdk import KiroAgentOptions
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app

FAKE = Path(__file__).with_name("fake_kiro_device.py").resolve()


@unittest.skipUnless(HAS_AGENTCORE, "requires the agentcore extra and a POSIX terminal")
class AgentCoreDeviceAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.apps = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    def app(self, **kwargs):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", cli_path=str(FAKE),
                                 auth_mode="device", startup_timeout=10, **kwargs)
        app = create_agentcore_app(config, options_factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], env={"HOME": "/wrong", "KIRO_API_KEY": "wrong"}))
        self.apps.append(app)
        return app

    async def invoke(self, app, payload, sid=b"session-a"):
        status, body, headers = await request(app, "POST", "/invocations", json.dumps(payload).encode(), [(HEADER, sid)])
        return status, json.loads(body), headers

    async def test_device_flow_is_session_scoped_and_logout_removes_authentication(self):
        app = self.app()
        status, result, headers = await self.invoke(app, {"action": "auth_start"})
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["user_code"], "ABCD-1234")
        self.assertEqual(result["verification_url"], "https://view.awsapps.com/start/#/device")
        _, repeated, _ = await self.invoke(app, {"action": "auth_start"})
        self.assertEqual(result, repeated)
        _, other, _ = await self.invoke(app, {"action": "auth_status"}, b"session-b")
        self.assertEqual(other["status"], "not_authenticated")
        _, denied, _ = await self.invoke(app, {"prompt": "before login"})
        self.assertTrue(denied["is_error"])
        self.assertEqual(denied["error"], "Device authentication required")
        session = app.state.kiro_agentcore.sessions["session-a"]
        login = session.device_login
        (session.directory / "home" / ".fake-approve").touch()
        await asyncio.wait_for(login.task, 5)
        self.assertIsNone(login.details)
        self.assertIs(login.challenge.result(), True)
        _, result, _ = await self.invoke(app, {"action": "auth_status"})
        self.assertEqual(result["status"], "authenticated")
        _, answer, _ = await self.invoke(app, {"prompt": "hello"})
        self.assertEqual(answer["response"], "hello")
        self.assertEqual(session.client.options.env["KIRO_API_KEY"], "")
        self.assertEqual(session.client.options.env["HOME"], str(session.directory / "home"))
        self.assertEqual(session.client.options.env["XDG_RUNTIME_DIR"], str(session.directory / "home" / ".runtime"))
        self.assertEqual((session.directory / "home" / ".runtime").stat().st_mode & 0o777, 0o700)
        _, result, _ = await self.invoke(app, {"action": "auth_logout"})
        self.assertEqual(result["status"], "signed_out")
        _, result, _ = await self.invoke(app, {"action": "auth_status"})
        self.assertEqual(result["status"], "not_authenticated")

    async def test_pending_authentication_cannot_be_evicted_and_is_reaped(self):
        app = self.app(max_sessions=1)
        _, result, _ = await self.invoke(app, {"action": "auth_start"})
        self.assertEqual(result["status"], "pending")
        login = app.state.kiro_agentcore.sessions["session-a"].device_login
        status, _, _ = await self.invoke(app, {"action": "auth_start"}, b"session-b")
        self.assertEqual(status, 429)
        _, result, _ = await self.invoke(app, {"action": "auth_cancel"})
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(login.task.done())
        self.assertIsNotNone(login.process.returncode)
        self.assertIsNone(login.details)
        _, result, _ = await self.invoke(app, {"action": "auth_status"}, b"session-b")
        self.assertEqual(result["status"], "not_authenticated")

    async def test_shutdown_cancels_pending_device_process(self):
        app = self.app()
        await self.invoke(app, {"action": "auth_start"})
        login = app.state.kiro_agentcore.sessions["session-a"].device_login
        await app.state.kiro_agentcore.close()
        self.assertTrue(login.task.done())
        self.assertIsNotNone(login.process.returncode)
        self.assertIsNone(login.details)

    async def test_pro_device_flow_and_untrusted_url(self):
        app = self.app(device_license="pro", device_start_url="https://org.awsapps.com/start", device_region="us-east-1")
        _, result, _ = await self.invoke(app, {"action": "auth_start"})
        self.assertEqual(result["status"], "pending")
        await self.invoke(app, {"action": "auth_cancel"})
        with patch.dict(os.environ, {"FAKE_DEVICE_BAD_URL": "1"}):
            _, result, _ = await self.invoke(app, {"action": "auth_start"})
        self.assertTrue(result["is_error"])
        self.assertNotIn("evil.example", json.dumps(result))

    async def test_device_actions_disabled_and_bad_settings_rejected(self):
        app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root, cli_path=str(FAKE), api_key="synthetic"))
        self.apps.append(app)
        status, _, _ = await self.invoke(app, {"action": "auth_start"})
        self.assertEqual(status, 403)
        for fields in ({"device_license":"pro"}, {"device_license":"pro", "device_start_url":"http://bad", "device_region":"us-east-1"},
                       {"device_license":"free", "device_start_url":"https://org.awsapps.com/start"},
                       {"device_timeout":float("nan")}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                AgentCoreConfig(allow_unauthorized_sessions=True, auth_mode="device", **fields)
