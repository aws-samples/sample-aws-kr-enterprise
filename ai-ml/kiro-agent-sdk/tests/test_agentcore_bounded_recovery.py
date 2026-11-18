import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request
from test_bounded_archive_recovery import archive_fixture

if HAS_STARLETTE:
    from kiro_agent_sdk import InMemorySessionStore, KiroAgentOptions, KiroSDKClient, project_key_for_directory
    from kiro_agent_sdk.agentcore import AgentCoreConfig, _write_marker, create_agentcore_app
    from kiro_agent_sdk.archive import _archive_payload


@unittest.skipUnless(HAS_STARLETTE, "install the agentcore extra")
class AgentCoreBoundedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / "state", cli_path=str(FAKE), api_key="synthetic",
                                      startup_timeout=10, turn_timeout=10)
        self.store = InMemorySessionStore()
        self.allowed = True
        self.apps = []
        session_dir = self.config.state_dir / hashlib.sha256(b"session-a").hexdigest()
        session_dir.mkdir(parents=True)
        self.workspace = session_dir / "workspace"
        self.workspace.mkdir()
        _write_marker(session_dir / "session.json", "v3", "source-id")
        self.key = {"project_key": project_key_for_directory(str(self.workspace)), "session_id": "source-id"}
        await self.store.append(self.key, [{"type": "session_archive",
                                           "data": _archive_payload("source-id", archive_fixture()[0])}])

    async def asyncTearDown(self):
        self.allowed = True
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    async def authorize(self, req, session):
        return self.allowed and req.headers.get("x-test-owner") == "yes" and session == "session-a"

    def app(self, *, authorizer=True):
        def options(workspace, resume):
            return KiroAgentOptions(tools=[], session_store=self.store, resume_from_store=resume is not None,
                                    resume_from_store_bounded_read=resume is not None)
        app = create_agentcore_app(self.config, options_factory=options,
                                   session_authorizer=self.authorize if authorizer else None)
        self.apps.append(app)
        return app

    async def invoke(self, app):
        return await request(app, "POST", "/invocations", b'{"prompt":"synthetic"}',
                             [(HEADER, b"session-a"), (b"x-test-owner", b"yes")], timeout=20)

    async def test_bounded_recovery_requires_authorizer(self):
        app = self.app(authorizer=False)
        with patch("kiro_agent_sdk.archive._load_archive_mirror") as load, \
                patch("kiro_agent_sdk.archive._import_session_archive") as importer:
            _, body, _ = await self.invoke(app)
        self.assertTrue(json.loads(body)["is_error"])
        load.assert_not_called()
        importer.assert_not_called()

    async def test_revocation_during_read_prevents_import(self):
        app = self.app()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.store.load_bounded
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        try:
            with patch.object(self.store, "load_bounded", delayed), \
                    patch("kiro_agent_sdk.archive._import_session_archive", return_value="restored-id") as importer:
                pending = asyncio.create_task(self.invoke(app))
                await asyncio.wait_for(entered.wait(), 10)
                self.allowed = False
                release.set()
                _, body, _ = await pending
            self.assertTrue(json.loads(body)["is_error"])
            self.assertNotIn(b"question", body)
            importer.assert_not_awaited()
        finally:
            release.set()

    async def test_changed_recovery_source_during_read_prevents_import(self):
        app = self.app()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.store.load_bounded
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        try:
            with patch.object(self.store, "load_bounded", delayed), \
                    patch("kiro_agent_sdk.archive._import_session_archive", return_value="restored-id") as importer:
                pending = asyncio.create_task(self.invoke(app))
                await asyncio.wait_for(entered.wait(), 10)
                client = app.state.kiro_agentcore.sessions["session-a"].client
                client.options.resume = "other-id"
                release.set()
                _, body, _ = await pending
            self.assertTrue(json.loads(body)["is_error"])
            importer.assert_not_awaited()
        finally:
            release.set()

    async def test_timed_out_recovery_read_remains_counted_until_it_finishes(self):
        app = self.app()
        service = app.state.kiro_agentcore
        service.config = replace(self.config, startup_timeout=2)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.store.load_bounded
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        try:
            with patch.object(self.store, "load_bounded", delayed), \
                    patch("kiro_agent_sdk.archive._import_session_archive", return_value="restored-id") as importer:
                pending = asyncio.create_task(self.invoke(app))
                await asyncio.wait_for(entered.wait(), 10)
                _, body, _ = await pending
                self.assertTrue(json.loads(body)["is_error"])
                self.assertEqual(len(service._session_recovery_operations), 1)
                self.assertFalse(service._session_history_operations)
                release.set()
                for _ in range(100):
                    if not service._session_recovery_operations:
                        break
                    await asyncio.sleep(0.01)
                self.assertFalse(service._session_recovery_operations)
                importer.assert_not_awaited()
        finally:
            release.set()

    async def test_cancelled_recovery_read_cannot_import_later(self):
        app = self.app()
        service = app.state.kiro_agentcore
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.store.load_bounded
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        try:
            with patch.object(self.store, "load_bounded", delayed), \
                    patch("kiro_agent_sdk.archive._import_session_archive", return_value="restored-id") as importer:
                pending = asyncio.create_task(self.invoke(app))
                await asyncio.wait_for(entered.wait(), 10)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                self.assertEqual(len(service._session_recovery_operations), 1)
                release.set()
                for _ in range(100):
                    if not service._session_recovery_operations:
                        break
                    await asyncio.sleep(0.01)
                self.assertFalse(service._session_recovery_operations)
                importer.assert_not_awaited()
        finally:
            release.set()

    async def test_recovery_capacity_is_separate_and_does_not_start_more_reads(self):
        app = self.app()
        service = app.state.kiro_agentcore
        pending = [asyncio.get_running_loop().create_future() for _ in range(8)]
        service._session_recovery_operations.update(pending)
        called = False
        async def unexpected(*args, **kwargs):
            nonlocal called
            called = True
            return []
        try:
            with patch.object(self.store, "load_bounded", unexpected), \
                    patch("kiro_agent_sdk.archive._import_session_archive") as importer:
                _, body, _ = await self.invoke(app)
            self.assertTrue(json.loads(body)["is_error"])
            self.assertFalse(called)
            self.assertFalse(service._session_history_operations)
            importer.assert_not_awaited()
        finally:
            service._session_recovery_operations.clear()
            for item in pending:
                item.cancel()

    async def test_revocation_during_import_does_not_start_a_turn(self):
        app = self.app()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return "restored-id"
        try:
            with patch("kiro_agent_sdk.archive._import_session_archive", side_effect=delayed):
                pending = asyncio.create_task(self.invoke(app))
                await asyncio.wait_for(entered.wait(), 10)
                client = app.state.kiro_agentcore.sessions["session-a"].client
                self.allowed = False
                release.set()
                _, body, _ = await pending
            self.assertTrue(json.loads(body)["is_error"])
            self.assertNotIn(b"question", body)
            self.assertEqual(client.restored_session_id, "restored-id")
            self.assertIsNone(client._turn)
        finally:
            release.set()

    async def test_revocation_during_session_load_hides_replay_and_prevents_turn(self):
        app = self.app()
        entered, release = asyncio.Event(), asyncio.Event()
        original = KiroSDKClient._request
        async def delayed(client, method, params, *args, **kwargs):
            if method == "session/load":
                entered.set()
                await release.wait()
            return await original(client, method, params, *args, **kwargs)
        try:
            with patch("kiro_agent_sdk.archive._import_session_archive", return_value="restored-id"), \
                    patch.object(KiroSDKClient, "_request", delayed):
                pending = asyncio.create_task(self.invoke(app))
                await asyncio.wait_for(entered.wait(), 10)
                client = app.state.kiro_agentcore.sessions["session-a"].client
                self.allowed = False
                release.set()
                _, body, _ = await pending
            self.assertTrue(json.loads(body)["is_error"])
            self.assertNotIn(b"synthetic", body)
            self.assertIsNone(client._turn)
        finally:
            release.set()

    async def test_authorized_recovery_completes(self):
        app = self.app()
        with patch("kiro_agent_sdk.archive._import_session_archive", return_value="restored-id") as importer:
            status, body, _ = await self.invoke(app)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["is_error"])
        importer.assert_awaited_once()
        self.assertEqual(app.state.kiro_agentcore.sessions["session-a"].client.session_id, "restored-id")

    async def test_revocation_after_response_headers_hides_recovered_output(self):
        app = self.app()
        incoming = [{"type": "http.request", "body": b'{"prompt":"synthetic"}', "more_body": False}]
        sent = []
        async def receive():
            if incoming:
                return incoming.pop(0)
            await asyncio.Event().wait()
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                self.allowed = False
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/invocations",
                 "raw_path": b"/invocations", "query_string": b"",
                 "headers": [(HEADER, b"session-a"), (b"x-test-owner", b"yes")],
                 "server": ("localhost", 8080), "client": ("localhost", 1)}
        with patch("kiro_agent_sdk.archive._import_session_archive", return_value="restored-id"):
            await asyncio.wait_for(app(scope, receive, send), 20)
        body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        self.assertTrue(json.loads(body)["is_error"])
        self.assertNotIn(b"synthetic", body)

    async def test_revocation_during_archive_validation_prevents_helper(self):
        from kiro_agent_sdk.archive import _check_archive
        app = self.app()
        started, release = threading.Event(), threading.Event()
        def delayed(*args):
            started.set()
            if not release.wait(10):
                raise TimeoutError("test did not release validation")
            return _check_archive(*args)
        try:
            with patch("kiro_agent_sdk.archive._check_archive", delayed), \
                    patch("kiro_agent_sdk.archive._helper") as helper:
                pending = asyncio.create_task(self.invoke(app))
                self.assertTrue(await asyncio.to_thread(started.wait, 10))
                self.allowed = False
                release.set()
                _, body, _ = await pending
            self.assertTrue(json.loads(body)["is_error"])
            helper.assert_not_awaited()
        finally:
            release.set()

    async def test_revocation_during_archive_write_prevents_helper(self):
        app = self.app()
        started, release = threading.Event(), threading.Event()
        original_write = Path.write_bytes
        def delayed_write(path, data):
            if path.name == "session.zip":
                started.set()
                if not release.wait(10):
                    raise TimeoutError("test did not release write")
            return original_write(path, data)
        try:
            with patch.object(Path, "write_bytes", delayed_write), \
                    patch("kiro_agent_sdk.archive._helper") as helper:
                pending = asyncio.create_task(self.invoke(app))
                self.assertTrue(await asyncio.to_thread(started.wait, 10))
                self.allowed = False
                release.set()
                _, body, _ = await pending
            self.assertTrue(json.loads(body)["is_error"])
            helper.assert_not_awaited()
        finally:
            release.set()
