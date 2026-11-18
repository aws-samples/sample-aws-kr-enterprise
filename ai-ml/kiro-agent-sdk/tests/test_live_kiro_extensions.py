"""Opt-in real-Kiro workflow and extension-method checks; these consume credits.

A case whose required Kiro behaviour is not reached is skipped (BLOCKED_PREREQUISITE), never
reported as PASS.
"""
import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from kiro_agent_sdk import (CLIConnectionError, JSONRPCError, KiroAgentOptions, KiroSDKClient, ProtocolError,
                            ResultMessage, delete_session)


def isolated_env(root: Path) -> dict:
    return {"KIRO_HOME": str(root / "kiro"), "XDG_STATE_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache")}


@unittest.skipUnless(os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli"),
                     "set KIRO_SDK_INTEGRATION=1 with an authenticated Kiro CLI")
class LiveKiroExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiro-live-ext-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.env = isolated_env(self.root)
        self.sessions = []

    async def asyncTearDown(self):
        for session_id, cwd in self.sessions:
            with contextlib.suppress(Exception):
                await delete_session(session_id, cwd=cwd, env=self.env)

    def track(self, client, cwd=None):
        if client.session_id:
            self.sessions.append((client.session_id, cwd or self.cwd))

    async def test_workflow_run_cancel_and_delete_apply_only_to_the_owned_run(self):
        recipe = {"name": "sdk-run-probe", "inputs": {}, "steps": [
            {"type": "step", "id": "probe", "agent": "wf-planner",
             "prompt": "Write a detailed 2000-word plan for a garden. Do not use tools."}]}
        async with KiroSDKClient(KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3",
                                                  enable_kiro_hooks=False)) as client:
            self.track(client)
            workflow_id = (await client.create_kiro_workflow(workflow=recipe))["workflowId"]
            try:
                self.assertIn(workflow_id, client._owned_workflows)
                started = await client.run_kiro_workflow(workflow_id)
                self.assertIsInstance(started, dict)
                states = []
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    inspected = await client.inspect_kiro_workflow(workflow_id)
                    self.assertEqual(inspected["workflowId"], workflow_id)
                    states.append(str(inspected.get("status") or inspected.get("state")))
                    if states[-1] not in ("pending", "created", "None"):
                        break
                    await asyncio.sleep(1)
                if states[-1] in ("pending", "created", "None"):
                    self.skipTest("Kiro did not start the workflow step")
                cancelled = await client.cancel_kiro_workflow(workflow_id)
                self.assertTrue(cancelled.get("ok"), cancelled)
                with self.assertRaises((JSONRPCError, ProtocolError, ValueError)):
                    await client.inspect_kiro_workflow("sdk-unknown-workflow-0000")
                with self.assertRaises(ValueError):
                    await client.run_kiro_workflow("")
                result = next(m for m in [m async for m in self._turn(client, "Reply with exactly OK. Use no tools.")]
                              if isinstance(m, ResultMessage))
                self.assertFalse(result.is_error)
            finally:
                deleted = await client.delete_kiro_workflow(workflow_id)
            self.assertTrue(deleted.get("ok"), deleted)
            self.assertNotIn(workflow_id, client._owned_workflows)
            listing = await client.list_kiro_workflows()
            self.assertFalse(any(run.get("workflowId") == workflow_id for run in listing.get("runs", [])))

    async def _turn(self, client, prompt):
        await client.query(prompt)
        async for message in client.receive_response():
            yield message

    async def test_extension_requests_report_unknown_and_invalid_calls_without_bypassing_guards(self):
        client = KiroSDKClient(KiroAgentOptions(cwd=self.cwd, env=self.env, agent_engine="v3", tools=[]))
        with self.assertRaises(CLIConnectionError):
            await client.extension_request("_kiro/memory/getSyncStatus", {})
        async with client:
            self.track(client)
            for method in ("session/prompt", "session/set_mode", "initialize", "kiro/memory/list"):
                with self.subTest(method=method):
                    with self.assertRaises(ValueError):
                        await client.extension_request(method, {"sessionId": client.session_id})
            with self.assertRaises(JSONRPCError) as unknown:
                await client.extension_request("_kiro/sdk-validation/doesNotExist", {})
            self.assertIsInstance(unknown.exception.code, int)
            with self.assertRaises(JSONRPCError):
                await client.extension_request("_kiro/memory/get", {"id": 7})
            status = await client.extension_request("_kiro/memory/getSyncStatus", {})
            self.assertIsInstance(status, dict)
            result = next(m for m in [m async for m in self._turn(client, "Reply with exactly OK. Use no tools.")]
                          if isinstance(m, ResultMessage))
            self.assertFalse(result.is_error)

if __name__ == "__main__":
    unittest.main()
