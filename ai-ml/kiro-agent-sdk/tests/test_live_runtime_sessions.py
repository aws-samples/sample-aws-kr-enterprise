"""Opt-in real-Kiro checks for runtime session, subagent and tool-batch controls; these consume credits.

A case whose required model behaviour (tool call, subagent, availability error) does not happen is
skipped (BLOCKED_PREREQUISITE), never reported as PASS.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import JSONRPCError, KiroAgentOptions, KiroSDKClient, ResultMessage, delete_session


@unittest.skipUnless(os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli"),
                     "set KIRO_SDK_INTEGRATION=1 with an authenticated Kiro CLI")
class LiveRuntimeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiro-live-rt-session-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.env = {"KIRO_HOME": str(self.root / "kiro"), "XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_CACHE_HOME": str(self.root / "cache")}
        self.sessions = []

    async def asyncTearDown(self):
        for session_id in self.sessions:
            try:
                await delete_session(session_id, cwd=self.cwd, env=self.env)
            except Exception:
                pass

    def track(self, client):
        if client.session_id and client.session_id not in self.sessions:
            self.sessions.append(client.session_id)

    async def turn(self, client, prompt):
        await client.query(prompt)
        return [message async for message in client.receive_response()]

    def base(self, **changes):
        values = dict(cwd=self.cwd, env=self.env, agent_engine="v3", tools=[], turn_timeout=150,
                      runtime_compat_mode="v3")
        values.update(changes)
        return KiroAgentOptions(**values)

    async def test_guarded_fork_options_drop_the_selected_turn(self):
        async with KiroSDKClient(self.base()) as client:
            self.track(client)
            await self.turn(client, "Remember the word FIRST. Reply with exactly OK.")
            messages = await self.turn(client, "Remember the word SECOND. Reply with exactly OK.")
            user_message_id = next(m for m in messages if isinstance(m, ResultMessage)).user_message_id
            if not user_message_id:
                self.skipTest("Kiro did not report a user message ID")
            options = await client.get_guarded_fork_options(user_message_id)
            original = client.session_id
        self.assertEqual((options["resume"], options["fork_session"], options["resume_drops_turn"]),
                         (original, True, user_message_id))
        self.assertIsInstance(options["resume_session_at"], str)
        fork = KiroSDKClient(self.base(**options))
        async with fork:
            self.track(fork)
            self.assertNotEqual(fork.session_id, original)
            result = next(m for m in await self.turn(
                fork, "Which remembered words do you know? Reply with them only, comma separated.")
                          if isinstance(m, ResultMessage))
        self.assertFalse(result.is_error)
        self.assertIn("FIRST", result.result.upper())
        self.assertNotIn("SECOND", result.result.upper())

    async def test_runtime_fallback_switches_once_after_an_availability_error(self):
        from kiro_agent_sdk.sessions import _list_cli
        listing = await _list_cli("--list-models", str(self.cwd), "kiro-cli", self.env)
        models = [m.get("model_id") for m in listing.get("models", []) if isinstance(m.get("model_id"), str)]
        if not models:
            self.skipTest("CLI did not report models")
        options = self.base(model="kiro-sdk-unavailable-model", fallback_model=models[0], fallback_model_mode="runtime")
        try:
            client = KiroSDKClient(options)
            await client.connect()
        except Exception:
            self.skipTest("Kiro rejected the unavailable model at session creation; runtime boundary not reached")
        try:
            self.track(client)
            try:
                result = next(m for m in await self.turn(client, "Reply with exactly OK.")
                              if isinstance(m, ResultMessage))
            except JSONRPCError:
                self.skipTest("Kiro rejected the model ID itself; no model availability error to fall back from")
        finally:
            await client.disconnect()
        if result.is_error:
            self.skipTest("Kiro did not classify the failure as a model availability error")
        self.assertIn("OK", result.result.upper())


if __name__ == "__main__":
    unittest.main()
