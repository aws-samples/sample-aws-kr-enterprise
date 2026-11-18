import asyncio
import json
import os
import tempfile
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import (CLIConnectionError, JSONRPCError, InMemorySessionStore, KiroAgentOptions, KiroSDKClient,
                            ProtocolError, ResultMessage)
from kiro_agent_sdk.kiro_client_auth import validate_client_token
from test_transport import FakeTransport

FAKE = str(Path(__file__).with_name("fake_kiro.py").resolve())
SECRET = "SYNTHETIC_PRIVATE_TOKEN"


def token():
    return {"accessToken": SECRET, "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "profileArn": "arn:aws:codewhisperer:us-east-1:123456789012:profile/synthetic"}


class KiroClientTokenValidationTests(unittest.TestCase):
    def test_accepts_bounded_token_and_timezone_expiry(self):
        value = token()
        self.assertEqual(validate_client_token(value), value)
        self.assertNotIn(SECRET, repr(KiroAgentOptions(kiro_access_token_provider=lambda: None)))

    def test_accepts_recognized_kiro_identity_metadata(self):
        for method in ("social", "IdC", "external_idp"):
            for provider in ("Google", "Github", "BuilderId", "Internal", "Enterprise", "ExternalIdp"):
                value = {**token(), "authMethod": method, "provider": provider}
                with self.subTest(method=method, provider=provider):
                    self.assertEqual(validate_client_token(value), value)

    def test_rejects_unsafe_responses_without_including_values_in_errors(self):
        now = datetime.now(timezone.utc)
        for value in (
            {}, None, {"accessToken": SECRET}, {**token(), "extra": SECRET},
            {**token(), "accessToken": SECRET + "\n"}, {**token(), "accessToken": "x" * 16385},
            {**token(), "expiresAt": "not-a-date-" + SECRET},
            {**token(), "expiresAt": (now + timedelta(seconds=100)).isoformat()},
            {**token(), "expiresAt": (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()},
            {**token(), "profileArn": SECRET},
            {**token(), "authMethod": SECRET},
            {**token(), "provider": SECRET},
            {**token(), "authMethod": "api_key"},
            {**token(), "provider": ["Enterprise"]},
        ):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError) as caught:
                validate_client_token(value, now=now)
            self.assertNotIn(SECRET, str(caught.exception))


class KiroClientAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.status = self.root / "auth-status.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def assert_sanitized_exception_chain(self, error):
        pending, seen = [error], set()
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            self.assertNotIn(SECRET, str(current))
            self.assertNotIn(SECRET, repr(vars(current)))
            pending.extend((current.__cause__, current.__context__))

    def options(self, provider, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3", tools=[],
                      kiro_auth_method="client", kiro_access_token_provider=provider,
                      env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_CLIENT_AUTH_INIT": "1",
                           "FAKE_CLIENT_AUTH_TURN": "1", "FAKE_EXPECT_TOKEN": SECRET,
                           "FAKE_AUTH_STATUS": str(self.status), "FAKE_LOG_REQUESTS": str(self.log),
                           "FAKE_AUTH_STDERR_SECRET": SECRET})
        values.update(changes)
        return KiroAgentOptions(**values)

    async def test_init_and_turn_auth_without_logs_or_mirror_secrets(self):
        calls = []
        async def provider():
            calls.append(True)
            return token()
        store = InMemorySessionStore()
        options = self.options(provider, session_store=store, session_store_flush="eager")
        self.assertNotIn(SECRET, repr(options))
        with patch.dict(os.environ, {"KIRO_API_KEY": "SYNTHETIC_INHERITED_KEY"}):
            async with KiroSDKClient(options) as client:
                await client.query("hello")
                messages = [item async for item in client.receive_response() if isinstance(item, ResultMessage)]
                self.assertEqual(messages[-1].result, "hello")
                entries = await store.load(client._store_key)
                self.assertNotIn(SECRET, repr(entries))
                self.assertNotIn(SECRET.encode(), bytes(client._stderr))
                self.assertEqual(client._configured_env()["KIRO_API_KEY"], "")
                with self.assertRaisesRegex(ProtocolError, "explicit model"):
                    await client.set_model(None)
        self.assertEqual(len(calls), 2)
        statuses = list(map(json.loads, self.status.read_text().splitlines()))
        self.assertEqual(len(statuses), 2)
        self.assertTrue(all(item == {"valid": True, "has_profile": True, "flag": True, "api_key_absent": True}
                            for item in statuses))
        self.assertNotIn(SECRET, self.log.read_text())
        self.assertNotIn(SECRET, self.status.read_text())

    async def test_identity_metadata_reaches_kiro_without_recording_token(self):
        async def provider():
            return {**token(), "authMethod": "IdC", "provider": "Enterprise"}
        store = InMemorySessionStore()
        options = self.options(provider, session_store=store, session_store_flush="eager")
        options.env.update(FAKE_EXPECT_AUTH_METHOD="IdC", FAKE_EXPECT_AUTH_PROVIDER="Enterprise")
        async with KiroSDKClient(options) as client:
            await client.query("hello")
            messages = [item async for item in client.receive_response() if isinstance(item, ResultMessage)]
            self.assertEqual(messages[-1].result, "hello")
            entries = await store.load(client._store_key)
            self.assertNotIn(SECRET, repr(entries))
        statuses = list(map(json.loads, self.status.read_text().splitlines()))
        self.assertEqual(len(statuses), 2)
        self.assertTrue(all(item["valid"] for item in statuses))
        self.assertNotIn(SECRET, self.log.read_text())
        self.assertNotIn(SECRET, self.status.read_text())

    async def test_idle_or_invalid_request_never_calls_provider(self):
        calls = []
        async def provider():
            calls.append(True)
            return token()
        options = self.options(provider)
        async with KiroSDKClient(options) as client:
            before = len(calls)
            record = {"id": "idle", "turn": None, "cancelled": False, "responded": False}
            with patch.object(client, "_send", new_callable=AsyncMock) as send:
                await client._handle_client_auth_request(
                    {"method": "_kiro/auth/getAccessToken", "params": {}}, record)
            self.assertEqual(len(calls), before)
            self.assertIn("error", send.await_args.args[0])
            record = {"id": "invalid", "turn": None, "cancelled": False, "responded": False}
            with patch.object(client, "_send", new_callable=AsyncMock) as send:
                await client._handle_client_auth_request(
                    {"method": "_kiro/auth/getAccessToken", "params": {"secret": SECRET}}, record)
            self.assertEqual(len(calls), before)
            self.assertNotIn(SECRET, repr(send.await_args.args[0]))
            self.assertEqual(len(client._client_auth_request_times), before)

    async def test_authorized_idle_refresh_is_opt_in(self):
        calls, checks = [], []
        async def provider():
            calls.append(True)
            return token()
        async def idle_authorizer():
            checks.append(True)
            return True
        options = self.options(provider, kiro_client_auth_idle_mode="authorized",
                               kiro_idle_token_authorizer=idle_authorizer)
        options.env["FAKE_CLIENT_AUTH_IDLE"] = "1"
        async with KiroSDKClient(options) as client:
            await client.query("hello")
            _ = [item async for item in client.receive_response()]
            async def wait():
                while not self.status.exists() or len(self.status.read_text().splitlines()) < 3:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), 3)
        self.assertEqual(len(calls), 3)
        self.assertGreaterEqual(len(checks), 2)
        self.assertTrue(all(item["valid"] for item in map(json.loads, self.status.read_text().splitlines())))
        self.assertNotIn(SECRET, self.log.read_text())

    async def test_revoked_idle_authorizer_never_calls_provider(self):
        calls = []
        async def provider():
            calls.append(True)
            return token()
        async def deny():
            return False
        options = self.options(provider, kiro_client_auth_idle_mode="authorized", kiro_idle_token_authorizer=deny)
        options.env["FAKE_CLIENT_AUTH_IDLE"] = "1"
        async with KiroSDKClient(options) as client:
            await client.query("hello")
            _ = [item async for item in client.receive_response()]
            async def wait():
                while len(self.status.read_text().splitlines()) < 3:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), 3)
        self.assertEqual(len(calls), 2)
        self.assertFalse(json.loads(self.status.read_text().splitlines()[-1])["valid"])

    async def test_idle_authorizer_exception_is_sanitized(self):
        calls = []
        async def provider():
            calls.append(True)
            return token()
        async def deny():
            raise ValueError("private " + SECRET)
        options = self.options(provider, kiro_client_auth_idle_mode="authorized", kiro_idle_token_authorizer=deny)
        options.env["FAKE_CLIENT_AUTH_IDLE"] = "1"
        async with KiroSDKClient(options) as client:
            await client.query("hello")
            _ = [item async for item in client.receive_response()]
            async def wait():
                while len(self.status.read_text().splitlines()) < 3:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), 3)
        self.assertEqual(len(calls), 2)
        self.assertNotIn(SECRET, self.status.read_text())
        self.assertNotIn(SECRET, self.log.read_text())

    async def test_cancel_ignoring_idle_authorizer_cannot_deliver_late_token(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        async def provider():
            calls.append(True)
            return token()
        async def idle_authorizer():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return True
        options = self.options(provider, kiro_client_auth_idle_mode="authorized",
                               kiro_idle_token_authorizer=idle_authorizer, kiro_access_token_timeout=1.0)
        options.env["FAKE_CLIENT_AUTH_IDLE"] = "1"
        client = KiroSDKClient(options)
        try:
            await client.connect()
            await client.query("hello")
            _ = [item async for item in client.receive_response()]
            await asyncio.wait_for(entered.wait(), 3)
            async def wait():
                while len(self.status.read_text().splitlines()) < 3:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), 3)
            self.assertFalse(json.loads(self.status.read_text().splitlines()[-1])["valid"])
            self.assertEqual(len(calls), 2)
            with self.assertRaisesRegex(CLIConnectionError, "authentication cleanup is still pending"):
                await client.disconnect()
            with self.assertRaisesRegex(CLIConnectionError, "authentication callback is still pending"):
                await client.connect()
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            self.assertNotIn(SECRET, self.log.read_text())
        finally:
            release.set()
            await client.disconnect()

    async def test_new_turn_cancels_idle_token_delivery(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def provider():
            calls.append(True)
            if len(calls) == 2:
                entered.set()
                await release.wait()
            return token()
        async def allow():
            return True
        options = self.options(provider, kiro_client_auth_idle_mode="authorized", kiro_idle_token_authorizer=allow)
        options.env.pop("FAKE_CLIENT_AUTH_TURN")
        options.env["FAKE_CLIENT_AUTH_IDLE"] = "1"
        async with KiroSDKClient(options) as client:
            await client.query("first")
            _ = [item async for item in client.receive_response()]
            await asyncio.wait_for(entered.wait(), 3)
            original_turn = client._turn
            next_turn = asyncio.create_task(asyncio.sleep(10))
            client._turn = next_turn
            try:
                release.set()
                async def wait():
                    while len(self.status.read_text().splitlines()) < 2:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(wait(), 3)
            finally:
                client._turn = original_turn
                next_turn.cancel()
                await asyncio.gather(next_turn, return_exceptions=True)
        statuses = list(map(json.loads, self.status.read_text().splitlines()))
        self.assertFalse(statuses[1]["valid"])

    async def test_new_turn_preparation_blocks_idle_token_delivery(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def provider():
            calls.append(True)
            if len(calls) == 2:
                entered.set()
                await release.wait()
            return token()
        async def allow():
            return True
        options = self.options(provider, kiro_client_auth_idle_mode="authorized", kiro_idle_token_authorizer=allow)
        options.env.pop("FAKE_CLIENT_AUTH_TURN")
        options.env["FAKE_CLIENT_AUTH_IDLE"] = "1"
        async with KiroSDKClient(options) as client:
            await client.query("first")
            _ = [item async for item in client.receive_response()]
            await asyncio.wait_for(entered.wait(), 3)
            await client._query_lock.acquire()
            try:
                release.set()
                async def wait():
                    while len(self.status.read_text().splitlines()) < 2:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(wait(), 3)
            finally:
                client._query_lock.release()
        self.assertFalse(json.loads(self.status.read_text().splitlines()[1])["valid"])

    async def test_rate_limit_rejects_excess_requests_without_calling_provider(self):
        calls = []
        async def provider():
            calls.append(True)
            return token()
        async with KiroSDKClient(self.options(provider)) as client:
            client._client_auth_request_times.clear()
            client._initializing = True
            try:
                with patch.object(client, "_send", new_callable=AsyncMock) as send:
                    for index in range(33):
                        record = {"id": str(index), "turn": None, "cancelled": False, "responded": False}
                        await client._handle_client_auth_request(
                            {"method": "_kiro/auth/getAccessToken", "params": {}}, record)
                self.assertEqual(len(calls), 33)  # Initial handshake plus 32 requests.
                self.assertIn("error", send.await_args_list[-1].args[0])
            finally:
                client._initializing = False

    async def test_provider_cannot_reenter_client_control(self):
        client, errors = None, []
        async def provider():
            for operation in (lambda: client.query("nested"), client.disconnect,
                              lambda: asyncio.create_task(client.disconnect())):
                try:
                    await operation()
                except CLIConnectionError as exc:
                    errors.append(str(exc))
            return token()
        client = KiroSDKClient(self.options(provider))
        async with client:
            self.assertEqual(len(errors), 3)
            self.assertTrue(all("cannot control" in message for message in errors))

    async def test_scope_mutation_during_provider_prevents_token_delivery(self):
        options = None
        async def provider():
            options.kiro_auth_method = "cli"
            return token()
        options = self.options(provider)
        client = KiroSDKClient(options)
        with self.assertRaises(Exception):
            await client.connect()
        self.assertFalse(json.loads(self.status.read_text().splitlines()[0])["valid"])
        self.assertNotIn(SECRET, self.log.read_text())

    async def test_provider_failure_is_sanitized(self):
        async def provider():
            raise ValueError("provider failure " + SECRET)
        options = self.options(provider)
        options.env["FAKE_AUTH_ERROR_SECRET"] = SECRET
        client = KiroSDKClient(options)
        with self.assertRaises(Exception) as caught:
            await client.connect()
        formatted = "".join(traceback.format_exception(type(caught.exception), caught.exception,
                                                        caught.exception.__traceback__))
        self.assertNotIn(SECRET, formatted)
        self.assert_sanitized_exception_chain(caught.exception)
        self.assertNotIn(SECRET, self.log.read_text())
        self.assertNotIn(SECRET, self.status.read_text())

    async def test_base_exception_from_provider_is_sanitized(self):
        async def provider():
            raise SystemExit(SECRET)
        client = KiroSDKClient(self.options(provider))
        with self.assertRaises(Exception) as caught:
            await client.connect()
        formatted = "".join(traceback.format_exception(type(caught.exception), caught.exception,
                                                        caught.exception.__traceback__))
        self.assertNotIn(SECRET, formatted)
        self.assert_sanitized_exception_chain(caught.exception)

    async def test_invalid_stdout_cannot_expose_raw_credentials(self):
        async def provider():
            return token()
        opts = self.options(provider)
        opts.env.update({"FAKE_BAD_JSON": "1", "FAKE_BAD_JSON_SECRET": SECRET})
        client = KiroSDKClient(opts)
        with self.assertRaises(Exception) as caught:
            await client.connect()
        formatted = "".join(traceback.format_exception(type(caught.exception), caught.exception,
                                                        caught.exception.__traceback__))
        self.assertNotIn(SECRET, formatted)
        self.assert_sanitized_exception_chain(caught.exception)

    async def test_cancel_ignoring_provider_never_sends_late_token(self):
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def provider():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return token()
        # Allow startup scheduling to reach the provider before its deadline.
        # A timeout before entry tests ordinary auth failure, not late tokens.
        client = KiroSDKClient(self.options(provider, kiro_access_token_timeout=1))
        try:
            # The rejected initialize RPC is the original error; failed
            # shutdown must not mask it with a cleanup exception.
            with self.assertRaises(JSONRPCError):
                await client.connect()
            self.assertTrue(entered.is_set())
            with self.assertRaisesRegex(CLIConnectionError, "authentication callback is still pending"):
                await client.connect()
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0)
            self.assertNotIn(SECRET, self.log.read_text())
            self.assertFalse(any(item["valid"] for item in map(json.loads, self.status.read_text().splitlines())))
        finally:
            release.set()
            await asyncio.gather(*tuple(client._client_auth_tasks), return_exceptions=True)
            await client.disconnect()

    async def test_requires_v3_local_provider_and_immutable_options(self):
        async def provider():
            return token()
        for changes in (
            {"agent_engine": "v2"},
            {"kiro_access_token_provider": None},
            {"kiro_auth_method": "invalid"},
            {"kiro_access_token_timeout": 0},
            {"kiro_client_auth_idle_mode": "authorized"},
            {"kiro_client_auth_idle_mode": "invalid"},
            {"stderr": lambda text: None},
            {"env": {"KIRO_API_KEY": "synthetic"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                KiroSDKClient(self.options(provider, **changes))
        with self.assertRaises(ValueError):
            KiroSDKClient(self.options(provider), transport=FakeTransport())
        options = self.options(provider)
        client = KiroSDKClient(options)
        options.kiro_access_token_provider = lambda: None
        with self.assertRaises(ValueError):
            await client.connect()
