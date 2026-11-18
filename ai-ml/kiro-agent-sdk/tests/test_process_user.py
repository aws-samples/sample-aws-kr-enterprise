import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport

if os.name == "posix":
    import pwd


FAKE = str(Path(__file__).with_name("fake_kiro.py").resolve())


@unittest.skipUnless(os.name == "posix", "POSIX process users are required")
class ProcessUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_uses_selected_current_account(self):
        account = pwd.getpwuid(os.geteuid()).pw_name
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(agent_engine="v3", cwd=root, cli_path=FAKE, user=account)
            async with KiroSDKClient(options) as client:
                await client.query("process_uid_probe")
                result = [message async for message in client.receive_response()][-1]
        self.assertEqual(result.result, str(os.geteuid()))

    async def test_default_does_not_request_a_process_user(self):
        from kiro_agent_sdk.client import _spawn_cli
        with patch("kiro_agent_sdk.client._spawn_cli", wraps=_spawn_cli) as spawn:
            async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", cli_path=FAKE)) as client:
                await client.query("hello")
                _ = [message async for message in client.receive_response()]
        self.assertNotIn("user", spawn.call_args.kwargs)

    async def test_selected_process_user_still_receives_configured_environment(self):
        account = pwd.getpwuid(os.geteuid()).pw_name
        options = KiroAgentOptions(agent_engine="v3", cli_path=FAKE, user=account,
                                   env={"TEST_KIRO_ENV": "synthetic-value"})
        async with KiroSDKClient(options) as client:
            await client.query("env_probe")
            result = [message async for message in client.receive_response()][-1]
        self.assertEqual(result.result, "synthetic-value")

    async def test_explicit_account_is_passed_to_process_launcher(self):
        from kiro_agent_sdk.client import _spawn_cli
        account = pwd.getpwuid(os.geteuid()).pw_name
        with patch("kiro_agent_sdk.client._spawn_cli", wraps=_spawn_cli) as spawn:
            async with KiroSDKClient(KiroAgentOptions(agent_engine="v3", cli_path=FAKE, user=account)):
                pass
        self.assertEqual(spawn.call_args.kwargs["user"], account)

    def test_other_account_gets_its_own_groups(self):
        from kiro_agent_sdk.client import _process_credentials
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no nobody account")
        with patch("kiro_agent_sdk.client.os.geteuid", return_value=0):
            credentials = _process_credentials(account.pw_name)
        self.assertEqual(credentials, {"group": account.pw_gid,
                                       "extra_groups": os.getgrouplist(account.pw_name, account.pw_gid)})
        current = pwd.getpwuid(os.geteuid())
        if os.geteuid() != 0:
            self.assertEqual(_process_credentials(current.pw_name), {})

    async def test_other_account_does_not_keep_parent_groups(self):
        if os.geteuid() != 0:
            self.skipTest("switching accounts requires root")
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no nobody account")
        from kiro_agent_sdk.client import _spawn_cli
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o755)
            secret = Path(root) / "group-only.txt"
            secret.write_text("synthetic")
            os.chmod(secret, 0o640)
            os.chown(secret, 0, 0)
            process = await _spawn_cli("/bin/sh", "-c", 'id -u; id -g; id -G; cat "$1" >/dev/null 2>&1 && echo read || echo denied',
                                       "probe", str(secret), stdout=asyncio.subprocess.PIPE,
                                       user=account.pw_name, start_new_session=True)
            output, _ = await process.communicate()
        uid, gid, groups, access = output.decode().split("\n")[:4]
        self.assertEqual((int(uid), int(gid)), (account.pw_uid, account.pw_gid))
        self.assertEqual(sorted(map(int, groups.split())),
                         sorted(set(os.getgrouplist(account.pw_name, account.pw_gid))))
        self.assertEqual(access, "denied")

    async def test_v2_continuation_listing_uses_the_same_account(self):
        account = pwd.getpwuid(os.geteuid()).pw_name
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(agent_engine="v2", cwd=root, cli_path=FAKE, user=account,
                                       continue_conversation=True)
            with patch("asyncio.create_subprocess_exec", wraps=asyncio.create_subprocess_exec) as spawn:
                async with KiroSDKClient(options) as client:
                    self.assertEqual(client.session_id, "one")
            self.assertGreaterEqual(spawn.call_count, 2)
            self.assertTrue(all(call.kwargs.get("user") == account for call in spawn.call_args_list))

    async def test_process_user_listing_rejects_invalid_data_and_deduplicates(self):
        from kiro_agent_sdk.sessions import _list_sessions_for_process_user
        for invalid in ({}, [None], [{"sessions": "bad"}], [{"sessions": [None]}]):
            with self.subTest(invalid=invalid), patch("kiro_agent_sdk.sessions._list_cli",
                                                       return_value=invalid):
                with self.assertRaises(ProtocolError):
                    await _list_sessions_for_process_user("/tmp", FAKE, {}, "selected")
        records = [{"sessionId": "one", "mtime": 1},
                   {"sessionId": "one", "mtime": 2},
                   {"sessionId": "two", "mtime": 3}]
        with patch("kiro_agent_sdk.sessions._list_cli", return_value=[{"sessions": records}]) as listing:
            sessions = await _list_sessions_for_process_user("/tmp", FAKE, {}, "selected")
        self.assertEqual([(item.session_id, item.updated_at) for item in sessions], [("two", 3), ("one", 2)])
        self.assertEqual(listing.call_args.kwargs["_process_user"], "selected")

    async def test_cancelling_process_user_listing_terminates_its_process(self):
        from kiro_agent_sdk.sessions import _list_sessions_for_process_user
        account = pwd.getpwuid(os.geteuid()).pw_name
        started = asyncio.Event()
        processes = []
        original = asyncio.create_subprocess_exec
        async def observe(*args, **kwargs):
            process = await original(*args, **kwargs)
            processes.append(process)
            started.set()
            return process
        with tempfile.TemporaryDirectory() as root:
            with patch("asyncio.create_subprocess_exec", side_effect=observe):
                task = asyncio.create_task(_list_sessions_for_process_user(
                    root, FAKE, {"FAKE_LIST_SESSIONS_HANG": "1"}, account))
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIsNotNone(await asyncio.wait_for(processes[0].wait(), 5))
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_unavailable_account_fails_without_fallback(self):
        account = "kiro_sdk_nonexistent_account_634782"
        try:
            pwd.getpwnam(account)
        except KeyError:
            pass
        else:
            self.skipTest("test account exists")
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3", cli_path=FAKE, user=account))
        with self.assertRaises(KeyError):
            await client.connect()
        self.assertIsNone(client._process)

    async def test_switch_to_other_account_fails_when_not_permitted(self):
        if os.geteuid() == 0:
            self.skipTest("test requires an unprivileged user")
        try:
            account = pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no nobody account")
        if account.pw_uid == os.geteuid():
            self.skipTest("already running as nobody")
        client = KiroSDKClient(KiroAgentOptions(agent_engine="v3", cli_path=FAKE, user=account.pw_name))
        with self.assertRaises(PermissionError):
            await client.connect()
        self.assertIsNone(client._process)

    async def test_invalid_account_and_custom_transport_fail(self):
        for user in ("", "  ", "bad\nuser", "\0", "\x85", "\ud800", True, 1, "x" * 257):
            with self.subTest(user=user), self.assertRaisesRegex(ValueError, "user"):
                KiroSDKClient(KiroAgentOptions(user=user))
        account = pwd.getpwuid(os.geteuid()).pw_name
        with self.assertRaisesRegex(ValueError, "local"):
            KiroSDKClient(KiroAgentOptions(user=account), transport=FakeTransport())

    async def test_windows_account_selection_is_rejected(self):
        client = KiroSDKClient(KiroAgentOptions())
        client.options.user = "account"
        with patch("kiro_agent_sdk.client.os.name", "nt"):
            with self.assertRaisesRegex(ValueError, "user"):
                client._validate_unsupported_limits()

    async def test_changing_account_after_client_creation_fails_before_prompt(self):
        account = pwd.getpwuid(os.geteuid()).pw_name
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            options = KiroAgentOptions(agent_engine="v3", cwd=root, cli_path=FAKE, user=account,
                                       env={"FAKE_LOG_REQUESTS": str(log)})
            async with KiroSDKClient(options) as client:
                options.user = None
                with self.assertRaisesRegex(ValueError, "user"):
                    await client.query("must-not-run")
            messages = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertFalse(any(message.get("method") == "session/prompt" for message in messages))


if __name__ == "__main__":
    unittest.main()
