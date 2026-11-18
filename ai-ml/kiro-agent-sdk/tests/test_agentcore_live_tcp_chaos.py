"""Opt-in AgentCore HTTP chaos over real loopback TCP against the authenticated Kiro CLI.

These cases need raw sockets (slow or abandoned bodies, abrupt disconnects) and therefore
always serve the app with uvicorn instead of calling it in process. They consume credits.
A required model behaviour that is not reached is skipped, never reported as PASS.
"""
import asyncio
from contextlib import contextmanager
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import tempfile
import time
import unittest

from kiro_agent_sdk import KiroAgentOptions, delete_session

AVAILABLE = (os.getenv("KIRO_SDK_INTEGRATION") == "1" and shutil.which("kiro-cli") and
             all(importlib.util.find_spec(name) for name in ("starlette", "uvicorn", "httpx")))
if AVAILABLE:
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"


class Served:
    def __init__(self, app):
        self.app = app

    async def __aenter__(self):
        import httpx
        import uvicorn

        class Server(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                yield
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.server = Server(uvicorn.Config(self.app, lifespan="off", log_level="critical", access_log=False))
        self.serving = asyncio.create_task(self.server.serve(sockets=[self.listener]))
        while not self.server.started:
            if self.serving.done():
                self.serving.result()
            await asyncio.sleep(.01)
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", timeout=200)
        return self

    async def call(self, runtime_id, payload, *, headers=None, timeout=200):
        headers = {**getattr(self, "default_headers", {}), **(headers or {})}
        response = await self.http.post("/invocations", content=json.dumps(payload).encode(), timeout=timeout,
                                        headers={HEADER: runtime_id, "content-type": "application/json",
                                                 **headers})
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"non_json_body_bytes": len(response.content)}

    async def raw(self):
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def __aexit__(self, *error):
        try:
            await self.http.aclose()
            self.server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(self.serving), 30)
            except asyncio.TimeoutError:
                self.server.force_exit = True
                await asyncio.wait_for(self.serving, 15)
        finally:
            self.listener.close()
            await self.app.state.kiro_agentcore.close()
        if self.app.state.kiro_agentcore.sessions:
            raise AssertionError("The TCP chaos app left an owned SDK session")


def abort(writer):
    # SO_LINGER 0 sends a TCP reset instead of an orderly close.
    sock = writer.get_extra_info("socket")
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()


@unittest.skipUnless(AVAILABLE, "requires authenticated Kiro CLI and the agentcore extra")
class AgentCoreTcpChaosTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kiro-tcp-chaos-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = []

    async def asyncTearDown(self):
        for session_id, workspace, env in self.sessions:
            with contextlib.suppress(Exception):
                await delete_session(session_id, cwd=workspace, env=env)

    def remember(self, app, runtime_id):
        session = app.state.kiro_agentcore.sessions.get(runtime_id)
        if session and session.client and session.client.session_id:
            self.sessions.append((session.client.session_id, session.directory / "workspace",
                                  dict(session.client.options.env)))

    def options_factory(self, **values):
        def options(workspace, resume):
            env = {"KIRO_HOME": str(workspace.parent / "kiro"), "XDG_STATE_HOME": str(workspace.parent / "state"),
                   "XDG_CONFIG_HOME": str(workspace.parent / "config"), "XDG_CACHE_HOME": str(workspace.parent / "cache")}
            return KiroAgentOptions(env=env, **{"tools": [], **values})
        return options

    def app(self, factory=None, **config):
        values = dict(state_dir=self.root / "state", auth_mode="cli", turn_timeout=120)
        values.update(config)
        return create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, **values), options_factory=factory or self.options_factory())

    async def test_slow_abandoned_and_oversized_bodies_stay_bounded_and_isolated(self):
        app = self.app(max_body_bytes=4096)
        async with Served(app) as served:
            slow_reader, slow = await served.raw()
            slow.write(b"POST /invocations HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
                       b"x-amzn-bedrock-agentcore-runtime-session-id: tcp-chaos-slow-body-0123456789abcdef\r\n"
                       b"Content-Length: 1000\r\n\r\n{\"prompt\":")
            await slow.drain()
            started = time.monotonic()
            status, body = await served.call("tcp-chaos-healthy-0123456789abcdef0123",
                                             {"prompt": "Reply with exactly READY. Do not use tools."})
            self.remember(app, "tcp-chaos-healthy-0123456789abcdef0123")
            self.assertEqual(status, 200, body)
            self.assertIn("READY", body["response"].upper())
            self.assertLess(time.monotonic() - started, 150)
            big_reader, big = await served.raw()
            big.write(b"POST /invocations HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
                      b"x-amzn-bedrock-agentcore-runtime-session-id: tcp-chaos-big-body-0123456789abcdef01\r\n"
                      b"Content-Length: 3000000\r\n\r\n")
            rejected = None
            with contextlib.suppress(ConnectionError):
                for _ in range(64):
                    big.write(b"x" * 8192)
                    await big.drain()
            with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
                rejected = await asyncio.wait_for(big_reader.readline(), 10)
            self.assertIsNotNone(rejected)
            self.assertIn(b" 413 ", rejected)
            abort(big)
            abort(slow)
            await asyncio.sleep(1)
            sessions = app.state.kiro_agentcore.sessions
            self.assertNotIn("tcp-chaos-slow-body-0123456789abcdef", sessions)
            self.assertNotIn("tcp-chaos-big-body-0123456789abcdef01", sessions)
            status, body = await served.call("tcp-chaos-healthy-0123456789abcdef0123",
                                             {"prompt": "Reply with exactly AGAIN. Do not use tools."})
            self.assertEqual(status, 200, body)

    async def test_abandoned_sse_consumer_leaves_a_reusable_session(self):
        app = self.app(allow_sdk_events=True)
        runtime_id = "tcp-chaos-sse-consumer-0123456789abcdef"
        async with Served(app) as served:
            reader, writer = await served.raw()
            body = json.dumps({"prompt": "Write a detailed 3000-word essay about bridges. Do not use tools.",
                               "stream": True, "include_events": True}).encode()
            writer.write(b"POST /invocations HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
                         b"Accept: text/event-stream\r\nx-amzn-bedrock-agentcore-runtime-session-id: " +
                         runtime_id.encode() + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            seen = b""
            deadline = time.monotonic() + 90
            while seen.count(b"data: ") < 3 and time.monotonic() < deadline:
                seen += await asyncio.wait_for(reader.read(4096), 90)
            if seen.count(b"data: ") < 3:
                self.skipTest("the stream did not produce events before the deadline")
            self.remember(app, runtime_id)
            abort(writer)
            # The application decides; the SDK must settle the turn and not leak a busy session.
            status, body = None, None
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                status, body = await served.call(runtime_id, {"prompt": "Reply with exactly AFTER. Do not use tools."})
                if status != 409:
                    break
                await asyncio.sleep(2)
            self.assertEqual(status, 200, body)
            self.assertFalse(body["is_error"])
            self.assertEqual(len(app.state.kiro_agentcore.sessions), 1)

    async def test_owner_revocation_blocks_a_queued_invocation_and_undelivered_output(self):
        owner = {"allowed": True}

        async def authorizer(request, session_id):
            return owner["allowed"] and request.headers.get("x-test-owner") == "owner"
        app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", auth_mode="cli", turn_timeout=120,
                                                   reauthorize_output=True),
                                   options_factory=self.options_factory(), session_authorizer=authorizer)
        runtime_id = "tcp-chaos-revocation-0123456789abcdef01"
        headers = {"x-test-owner": "owner"}
        async with Served(app) as served:
            status, body = await served.call(runtime_id, {"prompt": "Reply with exactly FIRST. Do not use tools."},
                                             headers=headers)
            self.assertEqual(status, 200, body)
            self.remember(app, runtime_id)
            client = app.state.kiro_agentcore.sessions[runtime_id].client
            sent = []
            original = client._send

            async def record(message):
                sent.append(message.get("method"))
                await original(message)
            client._send = record
            active = asyncio.create_task(served.call(runtime_id, {
                "prompt": "Write a detailed 2000-word essay about rivers. Do not use tools."}, headers=headers))
            while sent.count("session/prompt") < 1:
                await asyncio.sleep(.05)
            queued = asyncio.create_task(served.call(runtime_id, {"prompt": "Reply with exactly QUEUED."},
                                                     headers=headers))
            await asyncio.sleep(1)
            owner["allowed"] = False
            # Revocation hides undelivered output: either a denial or the SDK's generic structured
            # failure (never a 5xx that AgentCore could replay), and no model text.
            denied = ((403, {"error": "Session access denied"}), (200, {"error": "Agent invocation failed",
                                                                         "is_error": True}))
            self.assertIn(await asyncio.wait_for(active, 180), denied)
            status, body = await asyncio.wait_for(queued, 180)
            self.assertIn((status, body), (*denied, (409, body)))
            self.assertNotIn("response", body)
            self.assertEqual(sent.count("session/prompt"), 1)
            owner["allowed"] = True
            status, body = await served.call(runtime_id, {"prompt": "Reply with exactly LATER. Do not use tools."},
                                             headers=headers)
            self.assertEqual(status, 200, body)
            self.remember(app, runtime_id)

    async def test_interaction_deadline_discards_a_late_permission_approval(self):
        app = self.app(factory=self.options_factory(
            tools=["write"], kiro_permissions={"rules": [{"capability": "fs_write", "effect": "ask"}]}),
            allow_interactions=True, interaction_timeout=8)
        runtime_id = "tcp-chaos-late-approval-0123456789abcd"
        async with Served(app) as served:
            turn = asyncio.create_task(served.call(runtime_id, {
                "prompt": "Write TEST to probe.txt with the write tool. Use no other tools."}))
            pending = None
            deadline = time.monotonic() + 100
            while not turn.done() and time.monotonic() < deadline and pending is None:
                status, body = await served.call(runtime_id, {"action": "interactions"})
                items = body.get("pending", []) if status == 200 else []
                pending = items[0]["interaction_id"] if items else None
                await asyncio.sleep(.2)
            self.remember(app, runtime_id)
            if pending is None:
                turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
                self.skipTest("model did not request the write permission")
            await asyncio.sleep(10)
            status, body = await served.call(runtime_id, {"action": "respond", "interaction_id": pending,
                                                          "decision": "allow"})
            self.assertIn(status, (404, 409, 410), body)
            status, body = await asyncio.wait_for(turn, 150)
            self.assertEqual(status, 200, body)
            workspace = app.state.kiro_agentcore.sessions[runtime_id].directory / "workspace"
            self.assertFalse((workspace / "probe.txt").exists())

    async def test_remote_mcp_network_faults_fail_closed_without_retry_storms(self):
        hits = {}

        async def faulty(reader, writer):
            try:
                request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                path = request.split(b" ", 2)[1].decode()
                hits[path] = hits.get(path, 0) + 1
                code = {"/status-503": b"503 Service Unavailable", "/status-429": b"429 Too Many Requests",
                        "/status-401": b"401 Unauthorized"}.get(path, b"404 Not Found")
                writer.write(b"HTTP/1.1 " + code + b"\r\nRetry-After: 1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
        server = await asyncio.start_server(faulty, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        urls = {"503": f"http://127.0.0.1:{port}/status-503", "429": f"http://127.0.0.1:{port}/status-429",
                "401": f"http://127.0.0.1:{port}/status-401", "tls": f"https://127.0.0.1:{port}/status-tls",
                "dns": "http://kiro-sdk-validation.invalid/mcp"}
        selected = {}

        def options(workspace, resume):
            env = {"KIRO_HOME": str(workspace.parent / "kiro"), "XDG_STATE_HOME": str(workspace.parent / "state"),
                   "XDG_CONFIG_HOME": str(workspace.parent / "config"), "XDG_CACHE_HOME": str(workspace.parent / "cache")}
            return KiroAgentOptions(tools=[], env=env, sdk_mcp_toggle_mode="sdk-all",
                                    mcp_servers={"faulty": {"type": "http", "url": selected["url"]}})
        app = self.app(factory=options)
        try:
            async with Served(app) as served:
                for name, url in urls.items():
                    with self.subTest(fault=name):
                        selected["url"] = url
                        runtime_id = f"tcp-chaos-mcp-{name}-0123456789abcdef0123"
                        before = dict(hits)
                        started = time.monotonic()
                        status, body = await served.call(runtime_id, {
                            "prompt": "Reply with exactly MCPFAULT. Do not use tools."}, timeout=200)
                        self.remember(app, runtime_id)
                        self.assertLess(time.monotonic() - started, 180)
                        if status == 200:
                            # A connected session must report the failed server, not silently drop it.
                            status, mcp = await served.call(runtime_id, {"action": "mcp_status"})
                            servers = (mcp.get("result") or {}).get("mcpServers", [])
                            self.assertFalse(any(item.get("name") == "faulty" and item.get("status") == "connected"
                                                 for item in servers), mcp)
                        else:
                            self.assertGreaterEqual(status, 400)
                            self.assertNotIn("response", body)
                        path = "/status-" + name
                        self.assertLessEqual(hits.get(path, 0) - before.get(path, 0), 4)
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
