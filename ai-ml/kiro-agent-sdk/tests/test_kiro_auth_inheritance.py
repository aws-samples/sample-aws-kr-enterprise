import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import (KiroAgentOptions, KiroSDKClient, ProtocolError)
from test_kiro_client_auth import FAKE, SECRET, token


class KiroAuthInheritanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.status = self.root / "auth.jsonl"
        self.calls = 0

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def provider(self):
        self.calls += 1
        return token()

    def options(self, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3", tools=[],
                      kiro_auth_method="client", kiro_access_token_provider=self.provider,
                      env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_CLIENT_AUTH_INIT": "1",
                           "FAKE_CLIENT_AUTH_TURN": "1", "FAKE_EXPECT_TOKEN": SECRET,
                           "FAKE_CLIENT_AUTH_LIST": "1",
                           "FAKE_AUTH_STATUS": str(self.status), "FAKE_LOG_REQUESTS": str(self.log)})
        values.update(changes)
        return KiroAgentOptions(**values)

    def assert_safe_auth(self):
        statuses = [json.loads(line) for line in self.status.read_text().splitlines()]
        self.assertTrue(statuses)
        self.assertTrue(all(item["valid"] and item["flag"] and item["api_key_absent"] for item in statuses))
        self.assertNotIn(SECRET, self.log.read_text())
        self.assertNotIn(SECRET, self.status.read_text())

    def fork_options(self, **changes):
        skill = self.root / "demo" / "skills" / "review" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("---\ncontext: fork\ndisable-model-invocation: true\n---\nChild $ARGUMENTS")
        values = dict(plugins=[{"type": "local", "path": str(self.root / "demo")}],
                      plugin_loading_mode="portable", sdk_tool_interrupt_mode="cancel-observed",
                      skill_fork_options_factory=lambda name, agent: KiroAgentOptions(
                          tools=[], env={"FAKE_DEFAULT_SESSION_ID": "child-session"}))
        values.update(changes)
        return self.options(**values)


class ContinuationListingTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client = KiroSDKClient(KiroAgentOptions(cli_path=FAKE, agent_engine="v3"))
        client.server_info = {"agentCapabilities": {"sessionCapabilities": {"list": {}}}}
        return client

    async def test_selects_latest_across_pages(self):
        client = self.client()
        pages = [{"sessions": [{"sessionId": "old", "updatedAt": "2026-01-01T00:00:00Z"}],
                  "nextCursor": "next"},
                 {"sessions": [{"sessionId": "new", "updatedAt": "2026-02-01T00:00:00Z"}]}]
        with patch.object(client, "_request", new_callable=AsyncMock, side_effect=pages) as request:
            self.assertEqual(await client._find_latest_session("/workspace"), "new")
        self.assertEqual(request.await_args_list[1].args[1]["cursor"], "next")

    async def test_unique_cursor_stream_is_bounded(self):
        client = self.client()
        pages = [{"sessions": [], "nextCursor": str(index)} for index in range(101)]
        with patch.object(client, "_request", new_callable=AsyncMock, side_effect=pages) as request:
            with self.assertRaisesRegex(ProtocolError, "page limit"):
                await client._find_latest_session("/workspace")
        self.assertEqual(request.await_count, 100)

    async def test_cumulative_metadata_and_session_count_are_bounded(self):
        for page in ({"sessions": [{}] * 10001},
                     {"sessions": [{"sessionId": "one", "title": "x" * (4 * 1024 * 1024)}],
                      "nextCursor": "next"}):
            client = self.client()
            with patch.object(client, "_request", new_callable=AsyncMock, return_value=page) as request:
                with self.assertRaisesRegex(ProtocolError, "size limit"):
                    await client._find_latest_session("/workspace")
            self.assertLessEqual(request.await_count, 2)

    async def test_revocation_during_a_page_prevents_the_next_request(self):
        client = self.client()
        client._client_auth_control_active = True
        allowed = True
        async def authorize():
            return allowed
        async def page(*args):
            nonlocal allowed
            allowed = False
            return {"sessions": [{"sessionId": "one"}], "nextCursor": "next"}
        client._prompt_send_guard = authorize
        with patch.object(client, "_request", new_callable=AsyncMock, side_effect=page) as request:
            with self.assertRaises(PermissionError):
                await client._find_latest_session("/workspace")
        self.assertEqual(request.await_count, 1)
