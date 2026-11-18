"""AgentCoreConfig.device_timeout bounds a pending device login."""
import asyncio
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from test_agentcore import HEADER, request

HAS_AGENTCORE = importlib.util.find_spec("starlette") is not None and os.name == "posix"
if HAS_AGENTCORE:
    from kiro_agent_sdk import KiroAgentOptions
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app

FAKE = Path(__file__).with_name("fake_kiro_device.py").resolve()


@unittest.skipUnless(HAS_AGENTCORE, "requires the agentcore extra and a POSIX terminal")
class AgentCoreDeviceTimeoutContract(unittest.IsolatedAsyncioTestCase):
    async def test_unapproved_login_fails_after_the_configured_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory) / "state", cli_path=str(FAKE), auth_mode="device",
                                     startup_timeout=10, device_timeout=0.5)
            app = create_agentcore_app(config, options_factory=lambda workspace, resume: KiroAgentOptions(tools=[]))
            try:
                async def invoke(action):
                    status, body, _ = await request(app, "POST", "/invocations",
                                                    json.dumps({"action": action}).encode(), [(HEADER, b"session-a")])
                    return status, json.loads(body)
                self.assertEqual((await invoke("auth_start"))[1]["status"], "pending")
                started = time.monotonic()
                status = "pending"
                while status in ("pending", "starting") and time.monotonic() - started < 5:
                    await asyncio.sleep(0.05)
                    status = (await invoke("auth_status"))[1]["status"]
                self.assertEqual(status, "failed")
                self.assertLess(time.monotonic() - started, 3)
                login = app.state.kiro_agentcore.sessions["session-a"].device_login
                self.assertTrue(login.task.done())
                _, denied = await invoke("auth_status")
                self.assertEqual(denied["status"], "failed")
            finally:
                await app.state.kiro_agentcore.close()

    async def test_timeout_has_no_device_login_outside_device_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, values in (("api-key", {"api_key": "synthetic"}), ("cli", {"auth_mode": "cli"})):
                config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory) / name, cli_path=str(FAKE), device_timeout=0.5,
                                         **values)
                app = create_agentcore_app(config, options_factory=lambda workspace, resume: KiroAgentOptions(tools=[]))
                try:
                    for action in ("auth_start", "auth_status"):
                        with self.subTest(mode=name, action=action):
                            status, body, _ = await request(app, "POST", "/invocations",
                                                            json.dumps({"action": action}).encode(),
                                                            [(HEADER, b"session-a")])
                            self.assertEqual((status, json.loads(body)["error"]),
                                             (403, "Device authentication is not enabled"))
                    self.assertNotIn("session-a", app.state.kiro_agentcore.sessions)
                finally:
                    await app.state.kiro_agentcore.close()


if __name__ == "__main__":
    unittest.main()
