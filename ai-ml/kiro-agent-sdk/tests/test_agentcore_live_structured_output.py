"""Opt-in structured output through the AgentCore HTTP app over real TCP; these consume credits.

Foreground invocations return text. Schema-validated output reaches HTTP callers through a
background task whose manager retains structured results, and a failed attempt can only be
retried by an explicit, side-effect-acknowledging application request.
"""
import asyncio
import contextlib
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid

from kiro_agent_sdk import BackgroundTaskManager, KiroAgentOptions, delete_session, SQLiteBackgroundRequestStore

AVAILABLE = (os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli") and
             all(importlib.util.find_spec(name) for name in ("starlette", "uvicorn", "httpx")))
if AVAILABLE:
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
    from test_agentcore_live_tcp_chaos import Served

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "population_millions": {"type": "number"}},
          "required": ["city", "population_millions"], "additionalProperties": False}


@unittest.skipUnless(AVAILABLE, "requires authenticated Kiro CLI and the agentcore extra")
class AgentCoreStructuredOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiro-tcp-structured-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def app(self, schema):
        def env(workspace):
            return {"KIRO_HOME": str(workspace.parent / "kiro"), "XDG_STATE_HOME": str(workspace.parent / "state"),
                    "XDG_CONFIG_HOME": str(workspace.parent / "config"),
                    "XDG_CACHE_HOME": str(workspace.parent / "cache")}

        def options(workspace, resume):
            return KiroAgentOptions(tools=[], env=env(workspace))

        def background(workspace, session_id):
            return KiroAgentOptions(tools=[], env=env(workspace),
                                    output_format={"type": "json_schema", "schema": schema})
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", auth_mode="cli", turn_timeout=150,
                                 allow_background_task_results=True, allow_background_task_retry=True)
        async def owner(request, session_id):
            return request.headers.get("x-test-owner") == "owner"
        return create_agentcore_app(config, options_factory=options, background_task_options_factory=background,
            session_authorizer=owner,
            background_tasks_factory=lambda workspace, session_id: BackgroundTaskManager(
                max_tasks=4, max_retained_result_bytes=65536, retain_structured_output=True),
            background_request_store=SQLiteBackgroundRequestStore(self.root / "requests.sqlite"))

    async def wait(self, served, runtime_id, task_id):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            status, body = await served.call(runtime_id, {"action": "background_task_status", "task_id": task_id})
            task = body.get("task", {})
            if task.get("state") in ("completed", "failed", "cancelled"):
                return task
            await asyncio.sleep(2)
        self.fail("background task did not finish")

    async def test_schema_valid_output_and_acknowledged_retry_over_tcp(self):
        runtime_id = "tcp-structured-output-0123456789abcdef01"
        app = self.app(SCHEMA)
        sessions = []
        try:
            async with Served(app) as served:
                served.default_headers = {"x-test-owner": "owner"}
                status, body = await served.call(runtime_id, {"prompt": "Reply with exactly READY. Do not use tools."})
                self.assertEqual(status, 200, body)
                session = app.state.kiro_agentcore.sessions[runtime_id]
                sessions.append((session.client.session_id, session.directory / "workspace",
                                 dict(session.client.options.env)))
                request_id = uuid.uuid4().hex
                status, body = await served.call(runtime_id, {
                    "action": "background_task_start", "request_id": request_id,
                    "prompt": "Return Paris and its population in millions as the requested JSON object."})
                self.assertEqual(status, 200, body)
                task = await self.wait(served, runtime_id, body["task_id"])
                status, result = await served.call(runtime_id, {"action": "background_task_result",
                                                                "task_id": body["task_id"]})
                self.assertEqual(status, 200, result)
                if task["state"] != "completed":
                    # The application decides whether a failed attempt is retried; it must acknowledge
                    # that side effects of the first attempt are not undone.
                    status, retry = await served.call(runtime_id, {
                        "action": "background_task_retry", "request_id": uuid.uuid4().hex,
                        "previous_request_id": request_id, "acknowledge_side_effects": True,
                        "prompt": "Return Paris and its population in millions as the requested JSON object."})
                    self.assertEqual(status, 200, retry)
                    task = await self.wait(served, runtime_id, retry["task_id"])
                    status, result = await served.call(runtime_id, {"action": "background_task_result",
                                                                    "task_id": retry["task_id"]})
                self.assertEqual(task["state"], "completed")
                structured = result.get("structured_output")
                self.assertIsInstance(structured, dict, result)
                self.assertEqual(set(structured), {"city", "population_millions"})
                self.assertIn("paris", structured["city"].lower())
                self.assertIsInstance(structured["population_millions"], (int, float))
        finally:
            for session_id, workspace, env in sessions:
                with contextlib.suppress(Exception):
                    await delete_session(session_id, cwd=workspace, env=env)


if __name__ == "__main__":
    unittest.main()
