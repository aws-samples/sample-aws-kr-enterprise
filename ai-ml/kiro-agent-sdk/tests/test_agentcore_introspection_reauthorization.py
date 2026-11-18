import asyncio
import importlib.util
import json
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request

if HAS_STARLETTE:
    from kiro_agent_sdk import KiroAgentOptions
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
    if importlib.util.find_spec("uvicorn") is not None:
        import uvicorn


def decode_chunked(encoded):
    content = bytearray()
    while True:
        line, encoded = encoded.split(b"\r\n", 1)
        size = int(line, 16)
        if not size:
            return bytes(content)
        content.extend(encoded[:size])
        if encoded[size:size + 2] != b"\r\n":
            raise AssertionError("Invalid chunk framing")
        encoded = encoded[size + 2:]


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreIntrospectionReauthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.apps = []
        self.turns = []
        self.allowed = True

    async def asyncTearDown(self):
        self.allowed = True
        for app, task in self.turns:
            if not task.done():
                await self.invoke(app, {"action": "interrupt"})
                await asyncio.wait_for(task, 15)
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, session):
        return self.allowed and req.headers.get("x-test-identity") == "owner" and session == "session-a"

    def app(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / f"state-{len(self.apps)}", cli_path=str(FAKE),
            api_key="synthetic", allow_command_introspection=True, allow_session_introspection=True,
            allow_active_introspection=True, turn_timeout=20)
        app = create_agentcore_app(config, options_factory=lambda workspace, resume: KiroAgentOptions(
            tools=[], env={"FAKE_STARTUP_COMMANDS": json.dumps(
                [{"name": "PRIVATE-COMMAND", "description": "Synthetic private"}])}),
            session_authorizer=self.authorize)
        self.apps.append(app)
        return app

    async def invoke(self, app, payload):
        return await request(app, "POST", "/invocations", json.dumps(payload).encode(),
                             [(HEADER, b"session-a"), (b"x-test-identity", b"owner")], timeout=20)

    async def call_with_header_hook(self, app, payload, on_start):
        incoming = [{"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}]
        sent = []
        async def receive():
            if incoming:
                return incoming.pop(0)
            await asyncio.Event().wait()
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                on_start()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/invocations",
                 "raw_path": b"/invocations", "query_string": b"", "root_path": "",
                 "headers": [(HEADER, b"session-a"), (b"x-test-identity", b"owner")],
                 "server": ("localhost", 8080), "client": ("localhost", 1234)}
        await asyncio.wait_for(app(scope, receive, send), 20)
        start = next(item for item in sent if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        return start["status"], body, dict(start.get("headers", []))

    async def connect(self, app):
        status, body, _ = await self.invoke(app, {"prompt": "hello"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        return app.state.kiro_agentcore.sessions["session-a"]

    async def start_turn(self, app):
        task = asyncio.create_task(self.invoke(app, {"prompt": "cancel"}))
        self.turns.append((app, task))
        async def wait():
            while True:
                session = app.state.kiro_agentcore.sessions.get("session-a")
                if session and session.turn_ready and session.client and session.client._turn and not session.client._turn.done():
                    return session
                if task.done():
                    raise AssertionError("Synthetic turn finished before introspection")
                await asyncio.sleep(.01)
        return await asyncio.wait_for(wait(), 10)

    async def test_revocation_during_idle_lookup_hides_result_and_keeps_client(self):
        app = self.app()
        session = await self.connect(app)
        client = session.client
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed():
            entered.set()
            await release.wait()
            return [{"name": "PRIVATE-RESULT"}]
        try:
            with patch.object(client, "get_kiro_available_commands", delayed):
                pending = asyncio.create_task(self.invoke(app, {"action": "available_commands"}))
                await asyncio.wait_for(entered.wait(), 10)
                self.allowed = False
                release.set()
                status, body, _ = await asyncio.wait_for(pending, 10)
            self.assertEqual(status, 403)
            self.assertNotIn(b"PRIVATE-RESULT", body)
            self.assertIs(session.client, client)
            self.assertFalse(session.lock.locked())
            self.allowed = True
            self.assertEqual((await self.invoke(app, {"prompt": "after"}))[0], 200)
        finally:
            self.allowed = True
            release.set()

    async def test_owner_revoked_after_headers_never_receives_idle_result(self):
        app = self.app()
        session = await self.connect(app)
        held = []
        def on_start():
            held.append(session.lock.locked())
            self.allowed = False
        try:
            status, body, headers = await self.call_with_header_hook(app, {"action": "available_commands"}, on_start)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"error": "Session access denied", "is_error": True})
            self.assertNotIn(b"PRIVATE-COMMAND", body)
            self.assertEqual(held, [True])
            self.assertNotIn(b"content-length", headers)
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertFalse(session.lock.locked())
            self.assertIsNotNone(session.client)
        finally:
            self.allowed = True

    async def test_authorized_body_is_sent_without_fixed_length(self):
        app = self.app()
        session = await self.connect(app)
        status, body, headers = await self.call_with_header_hook(app, {"action": "available_commands"}, lambda: None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"], [{"name": "PRIVATE-COMMAND", "description": "Synthetic private"}])
        self.assertNotIn(b"content-length", headers)
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertFalse(session.lock.locked())

    async def test_owner_revoked_after_headers_never_receives_active_snapshot(self):
        app = self.app()
        session = await self.start_turn(app)
        client = session.client
        try:
            status, body, headers = await self.call_with_header_hook(
                app, {"action": "available_commands"}, lambda: setattr(self, "allowed", False))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"error": "Session access denied", "is_error": True})
            self.assertNotIn(b"PRIVATE-COMMAND", body)
            self.assertNotIn(b"content-length", headers)
            self.assertIs(session.client, client)
            self.assertFalse(client._turn.done())
        finally:
            self.allowed = True

    async def test_config_change_after_headers_hides_result(self):
        app = self.app()
        session = await self.connect(app)
        service = app.state.kiro_agentcore
        def on_start():
            service.config = replace(service.config, allow_command_introspection=False)
        status, body, _ = await self.call_with_header_hook(app, {"action": "available_commands"}, on_start)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"error": "Session access denied", "is_error": True})
        self.assertNotIn(b"PRIVATE-COMMAND", body)
        self.assertFalse(session.lock.locked())

    async def test_client_replaced_after_headers_hides_old_result(self):
        app = self.app()
        session = await self.connect(app)
        original = session.client
        try:
            status, body, _ = await self.call_with_header_hook(
                app, {"action": "available_commands"}, lambda: setattr(session, "client", None))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"error": "Session access denied", "is_error": True})
            self.assertNotIn(b"PRIVATE-COMMAND", body)
            self.assertFalse(session.lock.locked())
        finally:
            session.client = original

    async def test_reconnected_client_after_headers_hides_old_result(self):
        app = self.app()
        session = await self.connect(app)
        client = session.client
        service = app.state.kiro_agentcore
        calls = 0
        async def reconnect(req, runtime_id):
            nonlocal calls
            calls += 1
            if calls == 4:
                previous = client.session_id
                await client.disconnect()
                await client.connect()
                self.assertEqual(client.session_id, previous)
            return True
        service.session_authorizer = reconnect
        status, body, _ = await self.invoke(app, {"action": "available_commands"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"error": "Session access denied", "is_error": True})
        self.assertNotIn(b"PRIVATE-COMMAND", body)
        self.assertFalse(session.lock.locked())

    async def test_send_failure_releases_idle_lease(self):
        app = self.app()
        session = await self.connect(app)
        def fail():
            raise RuntimeError("synthetic send failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic send failure"):
            await self.call_with_header_hook(app, {"action": "available_commands"}, fail)
        self.assertFalse(session.lock.locked())
        self.assertEqual(app.state.kiro_agentcore.active, 0)
        self.assertIsNotNone(session.client)

    async def test_cancellation_during_output_authorization_releases_idle_lease(self):
        app = self.app()
        session = await self.connect(app)
        service = app.state.kiro_agentcore
        blocked, entered = False, asyncio.Event()
        async def authorize(req, runtime_id):
            if blocked:
                entered.set()
                await asyncio.Event().wait()
            return True
        service.session_authorizer = authorize
        def on_start():
            nonlocal blocked
            blocked = True
        pending = asyncio.create_task(self.call_with_header_hook(
            app, {"action": "available_commands"}, on_start))
        await asyncio.wait_for(entered.wait(), 10)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(session.lock.locked())
        self.assertEqual(service.active, 0)
        self.assertIsNotNone(session.client)

    async def test_authorizer_timeout_after_headers_does_not_release_late_data(self):
        app = self.app()
        session = await self.connect(app)
        service = app.state.kiro_agentcore
        service.config = replace(service.config, session_authorization_timeout=.1)
        blocked, cancelled, release = False, asyncio.Event(), asyncio.Event()
        async def authorize(req, runtime_id):
            if blocked:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
            return True
        service.session_authorizer = authorize
        def on_start():
            nonlocal blocked
            blocked = True
        try:
            status, body, _ = await self.call_with_header_hook(app, {"action": "available_commands"}, on_start)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"error": "Session access denied", "is_error": True})
            self.assertNotIn(b"PRIVATE-COMMAND", body)
            await asyncio.wait_for(cancelled.wait(), 5)
            self.assertFalse(session.lock.locked())
            release.set()
        finally:
            release.set()

    async def test_reauthorization_before_lookup_prevents_backend_access(self):
        app = self.app()
        session = await self.connect(app)
        service = app.state.kiro_agentcore
        calls = 0
        async def deny_after_first(req, runtime_id):
            nonlocal calls
            calls += 1
            return calls == 1
        service.session_authorizer = deny_after_first
        with patch.object(session.client, "get_kiro_available_commands") as lookup:
            status, _, _ = await self.invoke(app, {"action": "available_commands"})
        self.assertEqual(status, 403)
        lookup.assert_not_called()
        self.assertFalse(session.lock.locked())

    async def test_policy_cannot_be_enabled_during_initial_authorization(self):
        app = self.app()
        service = app.state.kiro_agentcore
        service.config = replace(service.config, allow_command_introspection=False)
        async def enable(req, runtime_id):
            service.config = replace(service.config, allow_command_introspection=True)
            return True
        service.session_authorizer = enable
        status, _, _ = await self.invoke(app, {"action": "available_commands"})
        self.assertEqual(status, 403)
        self.assertEqual(service.sessions, {})

    async def test_result_is_frozen_before_reauthorization_callback(self):
        app = self.app()
        session = await self.connect(app)
        service = app.state.kiro_agentcore
        result = [{"name": "BEFORE"}]
        calls = 0
        async def lookup():
            return result
        async def mutate(req, runtime_id):
            nonlocal calls
            calls += 1
            if calls >= 3:
                result[0]["name"] = "PRIVATE-AFTER"
            return True
        service.session_authorizer = mutate
        with patch.object(session.client, "get_kiro_available_commands", lookup):
            status, body, _ = await self.invoke(app, {"action": "available_commands"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"], [{"name": "BEFORE"}])
        self.assertNotIn(b"PRIVATE-AFTER", body)

    async def test_cached_request_body_is_available_to_each_reauthorization(self):
        app = self.app()
        await self.connect(app)
        service = app.state.kiro_agentcore
        checks = []
        async def authorize_body(req, runtime_id):
            value = json.loads(await req.body())
            checks.append(value)
            return runtime_id == "session-a" and value == {"action": "available_commands"}
        service.session_authorizer = authorize_body
        status, body, _ = await self.invoke(app, {"action": "available_commands"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        self.assertEqual(len(checks), 4)

    @unittest.skipUnless(importlib.util.find_spec("uvicorn") is not None, "install the AgentCore extra")
    async def test_http1_response_uses_valid_chunked_framing(self):
        app = self.app()
        await self.connect(app)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        sock.setblocking(False)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async def started():
                while not server.started:
                    if task.done():
                        raise AssertionError("Loopback server failed to start")
                    await asyncio.sleep(.01)
            await asyncio.wait_for(started(), 10)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = b'{"action":"available_commands"}'
            headers = (b"POST /invocations HTTP/1.1\r\nHost: localhost\r\n"
                       b"x-amzn-bedrock-agentcore-runtime-session-id: session-a\r\n"
                       b"x-test-identity: owner\r\nContent-Type: application/json\r\n"
                       b"Connection: close\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n")
            writer.write(headers + payload)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 10)
            writer.close()
            await writer.wait_closed()
            head, encoded = response.split(b"\r\n\r\n", 1)
            self.assertIn(b"HTTP/1.1 200", head)
            self.assertIn(b"transfer-encoding: chunked", head.lower())
            self.assertNotIn(b"content-length:", head.lower())
            self.assertEqual(json.loads(decode_chunked(encoded))["result"],
                             [{"name": "PRIVATE-COMMAND", "description": "Synthetic private"}])
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 10)
            finally:
                sock.close()

    @unittest.skipUnless(importlib.util.find_spec("uvicorn") is not None, "install the AgentCore extra")
    async def test_http1_revocation_after_headers_sends_only_error_body(self):
        app = self.app()
        await self.connect(app)
        service = app.state.kiro_agentcore
        entered, release = asyncio.Event(), asyncio.Event()
        allowed, calls = True, 0
        async def authorize(req, session):
            nonlocal calls
            calls += 1
            if calls == 4:
                entered.set()
                await release.wait()
            return allowed
        service.session_authorizer = authorize
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        sock.setblocking(False)
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async def started():
                while not server.started:
                    if task.done():
                        raise AssertionError("Loopback server failed to start")
                    await asyncio.sleep(.01)
            await asyncio.wait_for(started(), 10)
            reader, writer = await asyncio.open_connection("127.0.0.1", sock.getsockname()[1])
            payload = b'{"action":"available_commands"}'
            headers = (b"POST /invocations HTTP/1.1\r\nHost: localhost\r\n"
                       b"x-amzn-bedrock-agentcore-runtime-session-id: session-a\r\n"
                       b"x-test-identity: owner\r\nContent-Type: application/json\r\n"
                       b"Connection: close\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n")
            writer.write(headers + payload)
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            await asyncio.wait_for(entered.wait(), 10)
            allowed = False
            release.set()
            encoded = await asyncio.wait_for(reader.read(), 10)
            writer.close()
            await writer.wait_closed()
            self.assertIn(b"HTTP/1.1 200", head)
            self.assertIn(b"transfer-encoding: chunked", head.lower())
            self.assertEqual(json.loads(decode_chunked(encoded)), {"error": "Session access denied", "is_error": True})
            self.assertNotIn(b"PRIVATE-COMMAND", encoded)
        finally:
            release.set()
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 10)
            finally:
                sock.close()
