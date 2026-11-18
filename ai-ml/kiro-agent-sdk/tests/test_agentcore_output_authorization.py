import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_agentcore import FAKE, HEADER, HAS_STARLETTE, request

if HAS_STARLETTE:
    from kiro_agent_sdk import HookMatcher, KiroAgentOptions
    from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app


@unittest.skipUnless(HAS_STARLETTE, 'install the agentcore extra')
class AgentCoreOutputAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.apps = []

    async def asyncTearDown(self):
        for app in self.apps:
            await app.state.kiro_agentcore.close()
        self.temp.cleanup()

    def app(self, *, config_changes=None, option_changes=None, authorizer=None):
        barrier = self.root / f'barrier-{len(self.apps)}'
        barrier.mkdir()
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / f'state-{len(self.apps)}', cli_path=str(FAKE),
                                 api_key='synthetic', **(config_changes or {}))
        def factory(workspace, resume):
            values = dict(option_changes or {})
            values['env'] = {'FAKE_OUTPUT_BARRIER_DIR': str(barrier), **values.get('env', {})}
            return KiroAgentOptions(**values)
        app = create_agentcore_app(config, options_factory=factory, session_authorizer=authorizer)
        self.apps.append(app)
        return app, barrier

    async def wait_for(self, condition, timeout=20):
        async def wait():
            while not condition():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout)

    def start_request(self, app, *, stream=False, include_events=False, prompt='barrier_output', details=False,
                      on_send=None, asgi_version='2.3'):
        payload = {'prompt': prompt, 'stream': stream}
        if include_events:
            payload['include_events'] = True
        if details:
            payload['include_prompt_block_details'] = True
        body = json.dumps(payload).encode()
        incoming = [{'type': 'http.request', 'body': body, 'more_body': False}]
        never = asyncio.Event()
        sent = []
        async def receive():
            if incoming:
                return incoming.pop(0)
            await never.wait()
        async def send(message):
            sent.append(message)
            if on_send is not None:
                on_send(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': asgi_version}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': '/invocations', 'raw_path': b'/invocations',
                 'query_string': b'', 'root_path': '', 'headers': [(HEADER, b'session-a'),
                     (b'x-test-identity', b'owner')], 'server': ('localhost', 8080), 'client': ('localhost', 1234)}
        async def run():
            await app(scope, receive, send)
            status = next(m['status'] for m in sent if m['type'] == 'http.response.start')
            content = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
            return status, content
        return asyncio.create_task(run())

    async def test_config_requires_authorizer_and_parses_env(self):
        config = AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=self.root / 'state', cli_path=str(FAKE), api_key='synthetic',
                                 reauthorize_output=True)
        with self.assertRaises(ValueError):
            create_agentcore_app(config)
        with self.assertRaises(ValueError):
            replace(config, reauthorize_output='true')
        with patch.dict(os.environ, {'KIRO_AGENTCORE_REAUTHORIZE_OUTPUT': 'true',
                                     'KIRO_API_KEY': 'synthetic'}, clear=True):
            self.assertTrue(AgentCoreConfig.from_env().reauthorize_output)
        with patch.dict(os.environ, {'KIRO_AGENTCORE_REAUTHORIZE_OUTPUT': 'invalid',
                                     'KIRO_API_KEY': 'synthetic'}, clear=True):
            with self.assertRaises(ValueError):
                AgentCoreConfig.from_env()

    async def test_policy_cannot_be_relaxed_during_authorization_or_lease(self):
        for stage in ('authorization', 'lease'):
            with self.subTest(stage=stage):
                holder = {}
                async def authorize(req, session):
                    if stage == 'authorization':
                        service = holder['app'].state.kiro_agentcore
                        service.config = replace(service.config, reauthorize_output=False)
                    return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
                app, barrier = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
                holder['app'] = app
                if stage == 'lease':
                    service = app.state.kiro_agentcore
                    original = service._lease
                    async def mutate(runtime_id):
                        result = await original(runtime_id)
                        service.config = replace(service.config, reauthorize_output=False)
                        return result
                    service._lease = mutate
                status, _ = await asyncio.wait_for(self.start_request(app, prompt='hello'), 20)
                self.assertEqual(status, 403)
                self.assertEqual(app.state.kiro_agentcore.active, 0)
                self.assertFalse((barrier / 'started').exists())

    async def test_nonstream_drops_result_after_owner_revocation(self):
        state = {'allowed': True}
        async def authorize(req, session):
            return state['allowed'] and req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, barrier = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        task = self.start_request(app)
        try:
            await self.wait_for(lambda: (barrier / 'started').exists())
            state['allowed'] = False
            (barrier / 'release').touch()
            status, body = await asyncio.wait_for(task, 20)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)['is_error'])
            self.assertNotIn(b'PRIVATE-LATE', body)
            self.assertIsNone(app.state.kiro_agentcore.sessions['session-a'].client)
        finally:
            (barrier / 'release').touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_nonstream_rechecks_after_response_headers_before_body(self):
        state = {'allowed': True}
        async def authorize(req, session):
            return state['allowed'] and req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, _ = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        def on_send(message):
            if message['type'] == 'http.response.start':
                state['allowed'] = False
        status, body = await asyncio.wait_for(self.start_request(app, prompt='SENSITIVE-SYNTHETIC',
                                                                 on_send=on_send), 20)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['is_error'])
        self.assertNotIn(b'SENSITIVE-SYNTHETIC', body)
        self.assertIsNone(app.state.kiro_agentcore.sessions['session-a'].client)

    async def test_client_reconnected_after_headers_cannot_release_old_result(self):
        state, holder = {'reconnect': False, 'done': False}, {}
        async def authorize(req, session):
            if state['reconnect']:
                state['reconnect'] = False
                client = holder['app'].state.kiro_agentcore.sessions[session].client
                session_id = client.session_id
                await client.disconnect()
                client.options.resume = session_id
                await client.connect()
                state['done'] = True
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, _ = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        holder['app'] = app
        def on_send(message):
            if message['type'] == 'http.response.start':
                state['reconnect'] = True
        status, body = await asyncio.wait_for(self.start_request(app, prompt='SENSITIVE-SYNTHETIC',
                                                                 on_send=on_send), 20)
        self.assertTrue(state['done'])
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['is_error'])
        self.assertNotIn(b'SENSITIVE-SYNTHETIC', body)
        self.assertIsNone(app.state.kiro_agentcore.sessions['session-a'].client)

    async def test_client_replaced_after_headers_is_stopped_without_output(self):
        async def authorize(req, session):
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, _ = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        holder = {}
        def on_send(message):
            if message['type'] == 'http.response.start':
                session = app.state.kiro_agentcore.sessions['session-a']
                holder['client'] = session.client
                holder['process'] = session.client._process
                session.client = None
        status, body = await asyncio.wait_for(self.start_request(app, prompt='SENSITIVE-SYNTHETIC',
                                                                 on_send=on_send), 20)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['is_error'])
        self.assertNotIn(b'SENSITIVE-SYNTHETIC', body)
        self.assertIsNotNone(holder['process'].returncode)
        self.assertFalse(holder['client']._connected)

    async def test_client_disconnect_releases_guarded_response_lease(self):
        async def authorize(req, session):
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, _ = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        incoming = [{'type': 'http.request', 'body': b'{"prompt":"hello"}', 'more_body': False},
                    {'type': 'http.disconnect'}]
        async def receive():
            if incoming:
                return incoming.pop(0)
            await asyncio.Event().wait()
        async def send(message):
            pass
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1',
                 'method': 'POST', 'scheme': 'http', 'path': '/invocations', 'raw_path': b'/invocations',
                 'query_string': b'', 'root_path': '', 'headers': [(HEADER, b'session-a'),
                     (b'x-test-identity', b'owner')], 'server': ('localhost', 8080), 'client': ('localhost', 1234)}
        await asyncio.wait_for(app(scope, receive, send), 20)
        service = app.state.kiro_agentcore
        self.assertEqual(service.active, 0)
        self.assertFalse(service.sessions['session-a'].lock.locked())

    async def test_asgi_send_failure_closes_stream_before_releasing_lease(self):
        async def authorize(req, session):
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, barrier = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        def on_send(message):
            if message['type'] == 'http.response.body' and b'EARLY' in message.get('body', b''):
                raise OSError('synthetic disconnect')
        task = self.start_request(app, stream=True, on_send=on_send, asgi_version='2.4')
        try:
            with self.assertRaises(Exception):
                await asyncio.wait_for(task, 20)
            service = app.state.kiro_agentcore
            self.assertEqual(service.active, 0)
            self.assertFalse(service.sessions['session-a'].lock.locked())
            self.assertIsNone(service.sessions['session-a'].client)
        finally:
            (barrier / 'release').touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_authorized_stream_including_sdk_events_completes(self):
        calls = []
        async def authorize(req, session):
            calls.append(session)
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, _ = self.app(config_changes={'reauthorize_output': True, 'allow_sdk_events': True},
                          authorizer=authorize)
        status, body = await asyncio.wait_for(self.start_request(app, stream=True, include_events=True,
                                                                 prompt='hello'), 20)
        self.assertEqual(status, 200)
        self.assertIn(b'hello', body)
        self.assertIn(b'sdk_event', body)
        self.assertIn(b'\"type\": \"result\"', body)
        self.assertGreater(len(calls), 2)

    async def test_authorized_error_result_is_returned_after_client_cleanup(self):
        async def authorize(req, session):
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, _ = self.app(config_changes={'reauthorize_output': True},
                          option_changes={'env': {'FAKE_STOP_REASON': 'cancelled'}}, authorizer=authorize)
        status, body = await asyncio.wait_for(self.start_request(app, prompt='hello'), 20)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['is_error'])
        self.assertEqual(json.loads(body)['stop_reason'], 'cancelled')
        self.assertIsNone(app.state.kiro_agentcore.sessions['session-a'].client)

    async def test_stream_keeps_prior_output_but_drops_later_output_after_revocation(self):
        state, early = {'allowed': True}, asyncio.Event()
        async def authorize(req, session):
            return state['allowed'] and req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, barrier = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        def on_send(message):
            if b'EARLY' in message.get('body', b''):
                early.set()
        task = self.start_request(app, stream=True, on_send=on_send)
        try:
            await asyncio.wait_for(early.wait(), 20)
            state['allowed'] = False
            (barrier / 'release').touch()
            status, body = await asyncio.wait_for(task, 20)
            self.assertEqual(status, 200)
            self.assertIn(b'EARLY', body)
            self.assertNotIn(b'RIVATE-LATE', body)
            self.assertIn(b'Agent invocation failed', body)
        finally:
            (barrier / 'release').touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_revocation_between_event_and_delta_prevents_second_send(self):
        state = {'allowed': True, 'revoked': False}
        async def authorize(req, session):
            return state['allowed'] and req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, barrier = self.app(config_changes={'reauthorize_output': True, 'allow_sdk_events': True},
                                authorizer=authorize)
        def on_send(message):
            body = message.get('body', b'')
            if b'sdk_event' in body and b'EARLY' in body:
                state['allowed'] = False
                state['revoked'] = True
                (barrier / 'release').touch()
        task = self.start_request(app, stream=True, include_events=True, on_send=on_send)
        try:
            status, body = await asyncio.wait_for(task, 20)
            self.assertTrue(state['revoked'])
            self.assertEqual(status, 200)
            self.assertIn(b'sdk_event', body)
            self.assertNotIn(b'"type": "text_delta", "text": "EARLY"', body)
            self.assertNotIn(b'RIVATE-LATE', body)
        finally:
            (barrier / 'release').touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_removed_authorizer_and_changed_config_drop_output(self):
        for change in ('authorizer', 'config'):
            with self.subTest(change=change):
                async def authorize(req, session):
                    return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
                app, barrier = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
                task = self.start_request(app)
                try:
                    await self.wait_for(lambda: (barrier / 'started').exists())
                    service = app.state.kiro_agentcore
                    if change == 'authorizer':
                        service.session_authorizer = None
                    else:
                        service.config = replace(service.config, max_sessions=service.config.max_sessions + 1)
                    (barrier / 'release').touch()
                    _, body = await asyncio.wait_for(task, 20)
                    self.assertTrue(json.loads(body)['is_error'])
                    self.assertNotIn(b'PRIVATE-LATE', body)
                    if change == 'authorizer':
                        self.assertEqual((await request(app, 'POST', '/invocations', b'{"prompt":"hello"}',
                                          [(HEADER, b'session-a')]))[0], 403)
                finally:
                    (barrier / 'release').touch()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_config_changed_inside_output_authorizer_cannot_release_result(self):
        state, holder = {'mutate': False}, {}
        async def authorize(req, session):
            if state['mutate']:
                service = holder['app'].state.kiro_agentcore
                service.config = replace(service.config, reauthorize_output=False)
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, barrier = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        holder['app'] = app
        task = self.start_request(app)
        try:
            await self.wait_for(lambda: (barrier / 'started').exists())
            state['mutate'] = True
            (barrier / 'release').touch()
            _, body = await asyncio.wait_for(task, 20)
            self.assertTrue(json.loads(body)['is_error'])
            self.assertNotIn(b'PRIVATE-LATE', body)
        finally:
            (barrier / 'release').touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_replaced_client_is_not_allowed_to_leak_or_remain_running(self):
        async def authorize(req, session):
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, barrier = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        task = self.start_request(app)
        client = None
        try:
            await self.wait_for(lambda: (barrier / 'started').exists())
            session = app.state.kiro_agentcore.sessions['session-a']
            client = session.client
            process = client._process
            session.client = None
            (barrier / 'release').touch()
            _, body = await asyncio.wait_for(task, 20)
            self.assertTrue(json.loads(body)['is_error'])
            self.assertNotIn(b'PRIVATE-LATE', body)
            self.assertIsNotNone(process.returncode)
            self.assertFalse(client._connected)
        finally:
            (barrier / 'release').touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if client is not None and client._connected:
                await client.disconnect()

    async def test_changed_kiro_session_identity_cannot_release_output(self):
        async def authorize(req, session):
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, barrier = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        task = self.start_request(app)
        try:
            await self.wait_for(lambda: (barrier / 'started').exists())
            session = app.state.kiro_agentcore.sessions['session-a']
            session.client.session_id = 'different-kiro-session'
            (barrier / 'release').touch()
            _, body = await asyncio.wait_for(task, 20)
            self.assertTrue(json.loads(body)['is_error'])
            self.assertNotIn(b'PRIVATE-LATE', body)
            self.assertIsNone(session.client)
        finally:
            (barrier / 'release').touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_timeout_cannot_deliver_late_authorization_result(self):
        state = {'slow': False}
        async def authorize(req, session):
            if state['slow']:
                try:
                    await asyncio.sleep(0.4)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.2)
                    return True
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, barrier = self.app(config_changes={'reauthorize_output': True, 'session_authorization_timeout': 0.05},
                                authorizer=authorize)
        task = self.start_request(app)
        try:
            await self.wait_for(lambda: (barrier / 'started').exists())
            state['slow'] = True
            started = time.monotonic()
            (barrier / 'release').touch()
            _, body = await asyncio.wait_for(task, 5)
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(json.loads(body)['is_error'])
            self.assertNotIn(b'PRIVATE-LATE', body)
            await asyncio.sleep(0.3)
            self.assertIsNone(app.state.kiro_agentcore.sessions['session-a'].client)
        finally:
            (barrier / 'release').touch()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_prompt_block_details_are_not_sent_after_revocation(self):
        for stream, reauthorize in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(stream=stream, reauthorize=reauthorize):
                state = {'allowed': True}
                async def authorize(req, session):
                    return state['allowed'] and req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
                async def block(data, ident, context):
                    state['allowed'] = False
                    return {'block': 'SENSITIVE-REASON'}
                app, _ = self.app(config_changes={'reauthorize_output': reauthorize, 'allow_prompt_block_details': True},
                    option_changes={'hooks': {'promptSubmit': [HookMatcher(hooks=[block])]}}, authorizer=authorize)
                _, body = await asyncio.wait_for(self.start_request(app, stream=stream, prompt='user-text', details=True), 20)
                self.assertNotIn(b'SENSITIVE-REASON', body)
                self.assertNotIn(b'user-text', body)
                self.assertIn(b'Agent invocation failed', body)

    async def test_prompt_block_details_remain_available_to_authorized_owner(self):
        async def authorize(req, session):
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        async def block(data, ident, context):
            return {'block': 'APPROVED-REASON'}
        app, _ = self.app(config_changes={'reauthorize_output': True, 'allow_prompt_block_details': True},
            option_changes={'hooks': {'promptSubmit': [HookMatcher(hooks=[block])]}}, authorizer=authorize)
        _, body = await asyncio.wait_for(self.start_request(app, prompt='user-text', details=True), 20)
        self.assertIn(b'APPROVED-REASON', body)

    async def test_repeated_authorization_can_read_cached_request_body(self):
        bodies = []
        async def authorize(req, session):
            bodies.append(await req.body())
            return req.headers.get('x-test-identity') == 'owner' and session == 'session-a'
        app, _ = self.app(config_changes={'reauthorize_output': True}, authorizer=authorize)
        status, body = await asyncio.wait_for(self.start_request(app, prompt='hello'), 20)
        self.assertEqual((status, json.loads(body)['response']), (200, 'hello'))
        self.assertGreater(len(bodies), 1)
        self.assertEqual(len(set(bodies)), 1)
