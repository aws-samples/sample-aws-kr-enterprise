import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import ProtocolError, get_session_info, list_sessions
from kiro_agent_sdk.sessions import _list_global_v3


@unittest.skipUnless(shutil.which("git"), "git is required for worktree listing")
class SessionListingScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_worktrees_and_all_workspaces_are_scoped_and_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            root, branch, other = (Path(directory) / name for name in ("root", "branch", "other"))
            root.mkdir()
            other.mkdir()
            def git(*args):
                subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            git("init", "-q", str(root))
            git("-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-q", "--allow-empty", "-m", "initial")
            git("-C", str(root), "worktree", "add", "-q", "-b", "sdk-test", str(branch))

            def envelope(path, session_id, timestamp):
                return {"cwd": str(path), "complete": True, "sessions": [
                    {"sessionId": session_id, "updatedAt": timestamp, "title": session_id}
                ]}
            values = [
                envelope(root, "root-id", "2026-01-01T00:00:01Z"),
                envelope(branch, "branch-id", "2026-01-01T00:00:03Z"),
                envelope(other, "other-id", "2026-01-01T00:00:02Z"),
                envelope(other, "root-id", "2026-01-01T00:00:00Z"),
            ]
            async def fake_list(flag, cwd, cli_path, env, *, all_directories=False):
                if all_directories:
                    return values
                return [e for e in values if os.path.realpath(e["cwd"]) == os.path.realpath(str(cwd))]
            v3 = [{"sessionId": "v3-id", "cwd": str(other), "updatedAt": "2026-01-01T00:00:04Z"}]
            with patch("kiro_agent_sdk.sessions._list_cli", new=AsyncMock(side_effect=fake_list)) as mocked, \
                    patch("kiro_agent_sdk.sessions._list_global_v3", new=AsyncMock(return_value=v3)):
                worktrees = await list_sessions(root, 10, 0, True)
                self.assertEqual([s.session_id for s in worktrees], ["branch-id", "root-id"])
                self.assertEqual(worktrees[0].cwd, str(branch))
                self.assertEqual(mocked.await_count, 2)
                self.assertTrue(all(not c.kwargs.get("all_directories") for c in mocked.await_args_list))
                all_sessions = await list_sessions(all_directories=True, limit=2)
                self.assertEqual([s.session_id for s in all_sessions], ["v3-id", "branch-id"])
                self.assertEqual(len(await list_sessions(all_directories=True)), 4)
                self.assertEqual((await get_session_info("v3-id", all_directories=True)).cwd, str(other))
                self.assertEqual((await get_session_info("other-id", all_directories=True)).cwd, str(other))
                self.assertEqual([s.session_id for s in await list_sessions(root)], ["root-id"])

    async def test_non_git_directory_and_scope_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            records = [{"cwd": directory, "sessions": [{"sessionId": "s"}]}]
            with patch("kiro_agent_sdk.sessions._list_cli", new=AsyncMock(return_value=records)) as mocked:
                result = await list_sessions(directory, include_worktrees=True)
                self.assertEqual(result[0].session_id, "s")
                self.assertFalse(mocked.await_args.kwargs["all_directories"])
                with self.assertRaises(ValueError):
                    await list_sessions(directory, all_directories=True)
                with self.assertRaises(ValueError):
                    await list_sessions(include_worktrees=True, all_directories=True)
                with self.assertRaises(ValueError):
                    await list_sessions(include_worktrees="yes")


class GlobalV3ListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_and_cleanup(self):
        client = AsyncMock()
        client.list_sessions.side_effect = [
            {"sessions": [{"sessionId": "one"}], "nextCursor": "second"},
            {"sessions": [{"sessionId": "two"}]},
        ]
        with patch("kiro_agent_sdk.client.KiroSDKClient", return_value=client):
            records = await _list_global_v3("kiro-cli", None)
        self.assertEqual([r["sessionId"] for r in records], ["one", "two"])
        self.assertEqual([r["source"] for r in records], ["v3", "v3"])
        self.assertEqual(client.list_sessions.await_args_list[1].kwargs["cursor"], "second")
        client.disconnect.assert_awaited_once()

    async def test_repeated_cursor_fails_and_disconnects(self):
        client = AsyncMock()
        client.list_sessions.return_value = {"sessions": [], "nextCursor": "same"}
        with patch("kiro_agent_sdk.client.KiroSDKClient", return_value=client):
            with self.assertRaises(ProtocolError):
                await _list_global_v3("kiro-cli", None)
        client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
