import asyncio
import json
import tempfile
import traceback
import unittest
from pathlib import Path

from kiro_agent_sdk import CLIConnectionError, JSONRPCError, KiroSessionClient, ProtocolError
from test_kiro_client_auth import FAKE, SECRET, token


class SessionClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log, self.status = self.root / "requests.jsonl", self.root / "auth.jsonl"
        self.clients = []
        self.calls = 0

    async def asyncTearDown(self):
        for client in self.clients:
            await client.disconnect()
        self.temp.cleanup()

    async def provider(self):
        self.calls += 1
        return token()

    def client(self, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, access_token_provider=self.provider,
                      env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_CLIENT_AUTH_INIT": "1",
                           "FAKE_CLIENT_AUTH_LIST": "1", "FAKE_EXPECT_TOKEN": SECRET,
                           "FAKE_LOG_REQUESTS": str(self.log), "FAKE_AUTH_STATUS": str(self.status)})
        values.update(changes)
        client = KiroSessionClient(**values)
        self.clients.append(client)
        return client

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_safe_auth(self):
        records = [json.loads(line) for line in self.status.read_text().splitlines()]
        self.assertTrue(records)
        self.assertTrue(all(item["valid"] and item["flag"] and item["api_key_absent"] for item in records))
        self.assertNotIn(SECRET, self.log.read_text())
        self.assertNotIn(SECRET, self.status.read_text())

    async def test_authenticated_control_operations_do_not_create_a_chat(self):
        accesses = []
        async def authorize(access):
            accesses.append(access)
            return True
        async with self.client(authorize=authorize) as client:
            page = await client.list_sessions()
            self.assertEqual(page["sessions"][0]["sessionId"], "test-session")
            info = await client.get_session_info("test-session")
            self.assertEqual(info.session_id, "test-session")
            self.assertEqual(info.cwd, str(self.root))
            await client.rename_session("test-session", "new title")
            result = await client.fork_session("test-session")
            self.assertEqual(result.session_id, "forked")
            await client.delete_session("forked")
        self.assertFalse(any(item.get("method") in ("session/new", "session/load", "session/prompt")
                             for item in self.requests()))
        self.assertTrue(all(access.cwd == str(self.root) for access in accesses))
        self.assertTrue(any(access.operation == "delete_session" and access.session_id == "forked"
                            for access in accesses))
        self.assert_safe_auth()

    async def test_early_protocol_response_keeps_authorization_until_token_supply_finishes(self):
        for after_init in (True, False):
            with self.subTest(after_init=after_init):
                entered, release = asyncio.Event(), asyncio.Event()
                accesses = []
                async def authorize(access):
                    accesses.append(access.operation)
                    await asyncio.sleep(0)
                    return True
                async def provider():
                    entered.set()
                    await release.wait()
                    return token()
                client = self.client(authorize=authorize, access_token_provider=provider)
                client._client.options.env["FAKE_CLIENT_AUTH_INIT"] = ""
                client._client.options.env["FAKE_CLIENT_AUTH_LIST"] = ""
                client._client.options.env["FAKE_EARLY_INIT_RESPONSE" if after_init else
                                           "FAKE_EARLY_LIST_RESPONSE"] = "1"
                if not after_init:
                    await client.connect()
                operation = asyncio.create_task(client.connect() if after_init else client.list_sessions())
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    await asyncio.sleep(.05)
                    self.assertFalse(operation.done())
                    self.assertEqual(client._active.operation, "connect" if after_init else "list_sessions")
                    release.set()
                    await asyncio.wait_for(operation, 3)
                    self.assertEqual(client._client._client_auth_pending, 0)
                    await client.list_sessions()
                    self.assertTrue(accesses)
                    self.assert_safe_auth()
                finally:
                    release.set()
                    await asyncio.gather(operation, return_exceptions=True)
                    await client.disconnect()

    async def test_early_initialize_rechecks_revocation_before_sending_token(self):
        allowed = True
        async def authorize(access):
            return allowed
        async def provider():
            nonlocal allowed
            await asyncio.sleep(.02)
            allowed = False
            return token()
        client = self.client(authorize=authorize, access_token_provider=provider)
        client._client.options.env["FAKE_CLIENT_AUTH_INIT"] = ""
        client._client.options.env["FAKE_EARLY_INIT_RESPONSE"] = "1"
        sent_tokens = []
        send = client._client._send
        async def observe_send(message):
            if isinstance(message.get("result"), dict) and "accessToken" in message["result"]:
                sent_tokens.append(True)
            await send(message)
        client._client._send = observe_send
        with self.assertRaises(PermissionError):
            await client.connect()
        self.assertFalse(allowed)
        self.assertFalse(sent_tokens)
        self.assertFalse(client._connected)
        self.assertIsNone(client._client._process)
        # Shutdown can close the peer before it records the denial response.
        if self.status.exists():
            self.assertFalse(any(json.loads(line)["valid"] for line in self.status.read_text().splitlines()))

    async def test_cancelling_early_initialize_closes_the_waiting_authentication(self):
        entered = asyncio.Event()
        async def provider():
            entered.set()
            await asyncio.Event().wait()
        client = self.client(access_token_provider=provider)
        client._client.options.env["FAKE_CLIENT_AUTH_INIT"] = ""
        client._client.options.env["FAKE_EARLY_INIT_RESPONSE"] = "1"
        connecting = asyncio.create_task(client.connect())
        await asyncio.wait_for(entered.wait(), 3)
        connecting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await connecting
        self.assertFalse(client._connected)
        self.assertIsNone(client._client._process)

    async def test_cancel_at_control_initialization_completion_is_not_swallowed(self):
        client = self.client()
        connecting = None
        async def initialize():
            # Complete the inner awaitable in the same event-loop step that
            # cancels its caller (wait_for can lose this cancellation on 3.9–3.11).
            connecting.cancel()
        client._client._connect_control = initialize
        connecting = asyncio.create_task(client.connect())
        with self.assertRaises(asyncio.CancelledError):
            await connecting
        self.assertFalse(client._connected)
        self.assertIsNone(client._active)
        self.assertIsNone(client._client._process)

    async def test_cancel_at_policy_lock_acquisition_releases_lock(self):
        client = self.client()
        lock = client._client._client_auth_lock
        acquire = lock.acquire
        connecting = None
        async def acquire_and_cancel():
            acquired = await acquire()
            connecting.cancel()
            return acquired
        lock.acquire = acquire_and_cancel
        connecting = asyncio.create_task(client.connect())
        with self.assertRaises(asyncio.CancelledError):
            await connecting
        self.assertFalse(lock.locked())
        self.assertFalse(client._connected)
        self.assertFalse(self.requests())

    async def test_expired_rpc_budget_does_not_start_the_operation(self):
        client = self.client()
        started = []
        async def operation():
            started.append(True)
        client._deadline = asyncio.get_running_loop().time() - 1
        with self.assertRaisesRegex(CLIConnectionError, "timed out"):
            await client._rpc(operation())
        self.assertFalse(started)
        self.assertIsNone(client._client._process)

    async def test_history_and_messages_retain_provider_without_prompting(self):
        async with self.client() as client:
            events = await client.get_session_history("test-session")
            self.assertTrue(events)
            messages = await client.get_session_messages("test-session", coalesce_chunks=True)
            self.assertTrue(messages)
        methods = [item.get("method") for item in self.requests()]
        self.assertEqual(methods.count("session/load"), 2)
        self.assertNotIn("session/new", methods)
        self.assertNotIn("session/prompt", methods)
        self.assert_safe_auth()

    async def test_global_listing_and_other_session_require_authorization(self):
        async def authorize(access):
            return access.cwd is not None and access.session_id in (None, "owned")
        async with self.client(authorize=authorize) as client:
            before = len(self.requests())
            with self.assertRaises(PermissionError):
                await client.list_sessions(all_directories=True)
            with self.assertRaises(PermissionError):
                await client.delete_session("other")
            self.assertEqual(len(self.requests()), before)
            await client.list_sessions()

    async def test_revocation_after_token_supply_prevents_result_and_new_credentials(self):
        allowed = True
        async def authorize(access):
            return allowed
        async def provider():
            nonlocal allowed
            value = await self.provider()
            if self.calls == 2:
                allowed = False
            return value
        async with self.client(authorize=authorize, access_token_provider=provider) as client:
            with self.assertRaises(JSONRPCError):
                await client.list_sessions()
            self.assertEqual(self.calls, 2)
            with self.assertRaises(PermissionError):
                await client.list_sessions()
            self.assertEqual(self.calls, 2)
        self.assertFalse(json.loads(self.status.read_text().splitlines()[-1])["valid"])

    async def test_revocation_after_connection_cleans_up_even_when_aenter_fails(self):
        allowed = True
        async def authorize(access):
            return allowed
        client = self.client(authorize=authorize)
        original = client._client._connect_control
        async def connect():
            nonlocal allowed
            await original()
            allowed = False
        client._client._connect_control = connect
        with self.assertRaises(PermissionError):
            await client.__aenter__()
        self.assertFalse(client._connected)
        self.assertFalse(client._client._connected)
        self.assertIsNone(client._client._process)

    async def test_callbacks_and_descendants_cannot_reenter_control(self):
        client = None
        rejected = []
        async def authorize(access):
            if client is not None:
                for operation in (client.list_sessions, client.disconnect, client._client.interrupt):
                    with self.assertRaises(CLIConnectionError):
                        await asyncio.wait_for(asyncio.create_task(operation()), 2)
                    rejected.append(True)
            return True
        client = self.client(authorize=authorize)
        async with client:
            await client.list_sessions()
        self.assertTrue(rejected)

    async def test_cancel_resistant_policy_blocks_reuse_until_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def authorize(access):
            if access.operation == "list_sessions":
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            return True
        client = self.client(authorize=authorize)
        try:
            await client.connect()
            client._timeout = .1
            with self.assertRaises(PermissionError):
                await client.list_sessions()
            self.assertTrue(entered.is_set())
            with self.assertRaisesRegex(CLIConnectionError, "cleanup failure"):
                await client.list_sessions()
            with self.assertRaises(CLIConnectionError):
                await client.disconnect()
        finally:
            release.set()
            pending = list(client._client._client_auth_tasks)
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 3)
            await client.disconnect()
        self.assertFalse(any(item.get("method") == "session/list" for item in self.requests()))

    async def test_duplicate_connect_does_not_close_the_current_connection(self):
        async with self.client() as client:
            with self.assertRaisesRegex(CLIConnectionError, "Already connected"):
                await client.connect()
            self.assertTrue(client._connected)
            await client.list_sessions()

    async def test_idle_token_requests_are_rejected(self):
        client = self.client()
        client._client.options.env["FAKE_CLIENT_AUTH_IDLE"] = "1"
        async with client:
            # The fixture emits idle requests after a prompt; explicitly inject
            # the same unsolicited request to the idle control connection.
            from unittest.mock import AsyncMock, patch
            record = {"id": "idle", "turn": None, "cancelled": False, "responded": False}
            before = self.calls
            with patch.object(client._client, "_send", new_callable=AsyncMock) as send:
                await client._client._handle_client_auth_request(
                    {"params": {}, "method": "_kiro/auth/getAccessToken"}, record)
            self.assertEqual(self.calls, before)
            self.assertIn("error", send.await_args.args[0])

    async def test_policy_failure_does_not_disclose_callback_exception(self):
        async def authorize(access):
            raise ValueError(SECRET)
        client = self.client(authorize=authorize)
        with self.assertRaises(PermissionError) as caught:
            await client.connect()
        error = caught.exception
        self.assertNotIn(SECRET, "".join(traceback.format_exception(type(error), error, error.__traceback__)))
        self.assertEqual(self.calls, 0)

    async def test_reconnect_keeps_client_authentication(self):
        client = self.client()
        for _ in range(2):
            async with client:
                await client.list_sessions()
        self.assert_safe_auth()

    async def test_incoming_frames_are_bounded_before_returning_a_response(self):
        async with self.client() as client:
            client._client._incoming_message_limit = (1, 8 * 1024 * 1024)
            with self.assertRaises(ProtocolError):
                await client.list_sessions()
            with self.assertRaisesRegex(CLIConnectionError, "transport failure"):
                await client.list_sessions()

    async def test_zero_limit_does_not_resume_the_session(self):
        async with self.client() as client:
            self.assertEqual(await client.get_session_messages("test-session", limit=0), [])
        self.assertNotIn("session/load", [item.get("method") for item in self.requests()])

    async def test_changing_the_authorizer_is_rejected(self):
        async def authorize(access):
            return True
        async with self.client(authorize=authorize) as client:
            client._authorize = lambda access: asyncio.sleep(0, result=True)
            with self.assertRaises(PermissionError):
                await client.list_sessions()

    def test_configuration_validation(self):
        for timeout in (0, True, float("inf"), float("nan"), 10 ** 1000):
            with self.subTest(timeout_type=type(timeout).__name__), self.assertRaises(ValueError):
                KiroSessionClient(cli_path=FAKE, timeout=timeout)
        for environment in (False, [], {"KEY": 1}):
            with self.assertRaises(ValueError):
                KiroSessionClient(cli_path=FAKE, env=environment)

    async def test_rpc_timeout_closes_the_control_connection(self):
        client = self.client()
        client._client.options.env["FAKE_ACP_LIST_HANG"] = "1"
        await client.connect()
        client._timeout = .1
        with self.assertRaisesRegex(CLIConnectionError, "timed out"):
            await client.list_sessions()
        self.assertFalse(client._connected)
        self.assertIsNone(client._client._process)

    async def test_cancelling_submitted_mutation_closes_connection_before_reuse(self):
        client = self.client()
        client._client.options.env["FAKE_CONTROL_HANG"] = "1"
        await client.connect()
        operation = asyncio.create_task(client.rename_session("test-session", "cancelled"))
        try:
            async def submitted():
                while not any(item.get("method") == "_kiro/session/rename" for item in self.requests()):
                    await asyncio.sleep(.01)
            await asyncio.wait_for(submitted(), 3)
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertFalse(client._connected)
            self.assertIsNone(client._client._process)
            with self.assertRaisesRegex(CLIConnectionError, "Connect before"):
                await client.list_sessions()
            await client.connect()
            await client.list_sessions()
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)

    async def test_second_cancellation_does_not_abandon_shutdown(self):
        client = self.client()
        client._client.options.env["FAKE_ACP_LIST_HANG"] = "1"
        await client.connect()
        entered, release = asyncio.Event(), asyncio.Event()
        original = client._client.disconnect
        async def slow_disconnect():
            entered.set()
            await release.wait()
            await original()
        client._client.disconnect = slow_disconnect
        operation = asyncio.create_task(client.list_sessions())
        try:
            async def submitted():
                while not any(item.get("method") == "session/list" for item in self.requests()):
                    await asyncio.sleep(.01)
            await asyncio.wait_for(submitted(), 3)
            operation.cancel()
            await asyncio.wait_for(entered.wait(), 3)
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertIsNone(client._active)
            self.assertFalse(client._connected)
            with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                await client.connect()
            self.assertFalse(client._cleanup_task.cancelled())
        finally:
            release.set()
            await asyncio.wait_for(client._cleanup_task, 3)
            await asyncio.gather(operation, return_exceptions=True)
            client._client.disconnect = original
        self.assertIsNone(client._client._process)
        await client.connect()

    async def test_failed_shutdown_blocks_reconnect_until_explicit_cleanup(self):
        client = self.client()
        client._client.options.env["FAKE_ACP_LIST_HANG"] = "1"
        await client.connect()
        original = client._client.disconnect
        async def failed_disconnect():
            raise CLIConnectionError("simulated cleanup failure")
        client._client.disconnect = failed_disconnect
        client._timeout = .1
        try:
            with self.assertRaises(CLIConnectionError):
                await client.list_sessions()
            with self.assertRaisesRegex(CLIConnectionError, "cleanup failure"):
                await client.connect()
            self.assertFalse(client._connected)
        finally:
            client._client.disconnect = original
            await client.disconnect()
        client._timeout = 30
        await client.connect()

    async def test_cancelled_disconnect_continues_after_the_caller_leaves(self):
        client = self.client()
        await client.connect()
        entered, release = asyncio.Event(), asyncio.Event()
        original = client._client.disconnect
        async def slow_disconnect():
            entered.set()
            await release.wait()
            await original()
        client._client.disconnect = slow_disconnect
        closing = asyncio.create_task(client.disconnect())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            closing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closing
            self.assertTrue(client._closing)
            self.assertFalse(client._connected)
            with self.assertRaises(CLIConnectionError):
                await client.connect()
        finally:
            release.set()
            await asyncio.wait_for(client._disconnect_task, 3)
            client._client.disconnect = original
        self.assertIsNone(client._client._process)
        await client.connect()

    async def test_cancelled_history_cleanup_is_owned_until_child_exits(self):
        from unittest.mock import patch
        from kiro_agent_sdk import KiroSDKClient
        client = self.client()
        await client.connect()
        entered, release = asyncio.Event(), asyncio.Event()
        children = []
        original = KiroSDKClient.disconnect
        async def slow_child_disconnect(sdk_client):
            if sdk_client is not client._client:
                children.append(sdk_client)
                entered.set()
                await release.wait()
            await original(sdk_client)
        with patch.object(KiroSDKClient, "disconnect", slow_child_disconnect):
            operation = asyncio.create_task(client.get_session_history("test-session"))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                operation.cancel()
                async def shutdown_started():
                    while client._cleanup_task is None:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(shutdown_started(), 3)
                operation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
                self.assertTrue(client._history_clients)
                with self.assertRaisesRegex(CLIConnectionError, "cleanup is still pending"):
                    await client.connect()
            finally:
                release.set()
                await asyncio.wait_for(client._cleanup_task, 3)
                await asyncio.gather(operation, return_exceptions=True)
        self.assertTrue(children)
        self.assertTrue(all(child._process is None for child in children))
        self.assertFalse(client._history_clients)
        self.assertFalse(client._history_cleanup)
        self.assertIsNone(client._client._process)

    async def test_failed_history_cleanup_blocks_further_operations_until_disconnect(self):
        from unittest.mock import patch
        from kiro_agent_sdk import KiroSDKClient
        client = self.client()
        await client.connect()
        original = KiroSDKClient.disconnect
        async def fail_child_disconnect(sdk_client):
            if sdk_client is not client._client:
                raise CLIConnectionError("simulated child cleanup failure")
            await original(sdk_client)
        with patch.object(KiroSDKClient, "disconnect", fail_child_disconnect):
            with self.assertRaises(CLIConnectionError):
                await client.get_session_history("test-session")
            children = tuple(client._history_clients)
            self.assertTrue(children)
            with self.assertRaisesRegex(CLIConnectionError, "history cleanup failure"):
                await client.list_sessions()
            with self.assertRaises(CLIConnectionError):
                await client.disconnect()
            self.assertIsNone(client._client._process)
        await client.disconnect()
        self.assertTrue(all(child._process is None for child in children))
        self.assertFalse(client._history_clients)
        await client.connect()
