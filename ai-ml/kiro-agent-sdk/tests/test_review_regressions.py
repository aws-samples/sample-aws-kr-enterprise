"""Behavioral regressions from the Astra/Opus v1 audit of ad4b7b6."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from typing import Annotated
from typing_extensions import TypedDict, Required, NotRequired

from kiro_agent_sdk import (KiroAgentOptions, KiroSDKClient, HookMatcher, InMemorySessionStore,
    create_mcp_server, tool)
from kiro_agent_sdk.hooks import NativeHookBridge
from kiro_agent_sdk.mcp import SdkMcpBridge, _schema
from kiro_agent_sdk.mcp_output_tracker import McpOutputTracker
from kiro_agent_sdk.mcp_cancellation import ObservedToolCalls
from kiro_agent_sdk.kiro_client_auth import validate_client_token
from kiro_agent_sdk.transport import Transport

FAKE = str(Path(__file__).with_name('fake_kiro.py').resolve())

class OptionalInput(TypedDict):
    name: str
    limit: NotRequired[int]

class RequiredInput(TypedDict, total=False):
    name: Required[str]

class InheritedInput(RequiredInput):
    limit: Annotated[NotRequired[int], 'Optional limit']
    count: Annotated[Required[int], 'Required count']

class ScriptTransport(Transport):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.ready = False
        self.requests = []
        self.control_started = asyncio.Event()
        self.permission_replies = []
    async def connect(self): self.ready = True
    def is_ready(self): return self.ready
    async def close(self):
        self.ready = False
        self.queue.put_nowait(None)
    async def end_input(self): pass
    async def read_messages(self):
        while True:
            message = await self.queue.get()
            if message is None: return
            yield message
    async def write(self, data):
        wire = json.loads(data)
        self.requests.append(wire)
        method = wire.get('method')
        if method is None:
            self.permission_replies.append(wire)
            return
        if method == 'initialize':
            result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True,
                      '_meta': {'kiro': {'extensionMethods': ['_kiro/session/compact']}}}}
        elif method == 'session/new':
            result = {'sessionId': 'remote', 'modes': {'currentModeId': 'vibe'},
                      'configOptions': [{'id': 'model', 'currentValue': 'auto'}]}
        elif method == 'session/set_config_option':
            result = {'configOptions': [{'id': 'model', 'currentValue': wire['params']['value']}]}
        elif method == '_kiro/session/compact':
            self.control_started.set()
            return
        elif method == '_kiro/test/value':
            result = wire['params']['value']
        else:
            result = {}
        self.queue.put_nowait({'jsonrpc': '2.0', 'id': wire['id'], 'result': result})

class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_shared_options_do_not_share_runtime_permission_or_model_changes(self):
        options = KiroAgentOptions(cwd='/tmp', agent_engine='v3')
        first, second = ScriptTransport(), ScriptTransport()
        async with KiroSDKClient(options, transport=first) as a, KiroSDKClient(options, transport=second) as b:
            await a.set_permission_mode('trustAll')
            await a.set_model('other-model')
            self.assertEqual(a.sdk_permission_mode, 'trustAll')
            self.assertEqual(b.sdk_permission_mode, 'ask')
            self.assertEqual(options.permission_mode, 'ask')
            self.assertIsNone(options.model)
            request = {'jsonrpc': '2.0', 'id': 'permission', 'method': 'session/request_permission', 'params': {
                'sessionId': b.session_id, 'toolCall': {'toolCallId': 'call', 'title': 'shell'},
                'options': [{'optionId': 'allow', 'kind': 'allow_once'}, {'optionId': 'deny', 'kind': 'reject_once'}]}}
            second.queue.put_nowait(request)
            for _ in range(100):
                if second.permission_replies: break
                await asyncio.sleep(.01)
            self.assertEqual(second.permission_replies[-1]['result']['outcome']['optionId'], 'deny')
        self.assertEqual(KiroSDKClient(options).sdk_permission_mode, 'ask')

    async def test_evaluated_typed_dict_required_fields_gate_actual_handler(self):
        seen = []
        @tool('optional', 'Optional', OptionalInput)
        async def optional(args):
            seen.append(args)
            return {'content': []}
        @tool('required', 'Required', RequiredInput)
        async def required(args):
            seen.append(args)
            return {'content': []}
        bridge = SdkMcpBridge(create_mcp_server('review', tools=[optional, required]))
        try:
            response = await bridge._dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'optional', 'arguments': {'name': 'demo'}}})
            self.assertNotIn('error', response)
            response = await bridge._dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'required', 'arguments': {}}})
            self.assertTrue(response['result']['isError'])
            self.assertEqual(seen, [{'name': 'demo'}])
            self.assertEqual(set(_schema(InheritedInput)['required']), {'name', 'count'})
        finally:
            await bridge.close()

    async def native(self, event, hook, policy=None, supports_ask=True):
        bridge = NativeHookBridge({event: [HookMatcher(hooks=[hook], timeout=.1, on_failure=policy)]},
                                   supports_ask=supports_ask)
        try:
            _, env = await bridge.start()
            reader, writer = await asyncio.open_connection('127.0.0.1', int(env['KIRO_SDK_HOOK_PORT']))
            writer.write((env['KIRO_SDK_HOOK_TOKEN']+'\n'+json.dumps({'hook_event_name':event,'tool_name':'shell'})+'\n').encode())
            await writer.drain()
            result = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            return result
        finally:
            await bridge.close()

    async def test_native_restrictions_survive_invalid_extra_output(self):
        for decision in ('block', 'ask'):
            for extra in ({'unknown': True}, {}):
                async def hook(*args):
                    return {**extra, decision: 'restricted', 'context': 'unsupported extra context'}
                response = await self.native('PreToolUse', hook, 'continue', False)
                self.assertTrue(response['block'] or response.get('ask') == 'restricted')

    async def test_failed_stop_does_not_request_another_model_turn(self):
        async def fail(*args): raise RuntimeError('synthetic')
        self.assertFalse((await self.native('Stop', fail))['block'])

    async def test_cancellations_release_active_tracker_slots_and_suppress_late_results(self):
        output = []
        async def send(wire): output.append(wire)
        async def transform(*args): self.fail('Cancelled response must not be transformed')
        tracker = McpOutputTracker(transform, send, lambda: self.fail('Unexpected disconnect'))
        try:
            for ident in range(260):
                await tracker.request({'jsonrpc':'2.0','id':ident,'method':'tools/call',
                                       'params':{'name':'test','arguments':{}}})
                await tracker.cancel(ident)
            self.assertEqual(len(tracker._pending), 0)
            await tracker.response({'jsonrpc':'2.0','id':0,'result':{'content':[]}})
            self.assertEqual(len(output), 260)
            self.assertTrue(all(item['error']['code'] == -32800 for item in output))
        finally:
            await tracker.close()
        observed = ObservedToolCalls()
        for ident in range(260):
            observed.request({'id':ident,'method':'tools/call'})
            observed.request({'method':'notifications/cancelled','params':{'requestId':ident}})
        observed.request({'id':'new','method':'tools/call'})
        self.assertEqual(observed.ids(), ['new'])
        self.assertFalse(observed.response({'id':0,'result':{}}))

    async def test_disconnect_interrupts_a_nonresponding_control(self):
        transport = ScriptTransport()
        client = KiroSDKClient(KiroAgentOptions(cwd='/tmp', agent_engine='v3'), transport=transport)
        await client.connect()
        control = asyncio.create_task(client.compact())
        try:
            await asyncio.wait_for(transport.control_started.wait(), 2)
            await asyncio.wait_for(client.disconnect(), 3)
            self.assertFalse(transport.ready)
            self.assertTrue(control.done())
        finally:
            control.cancel()
            await asyncio.gather(control, return_exceptions=True)
            await client.disconnect()

    async def test_jsonrpc_preserves_falsy_results(self):
        async with KiroSDKClient(KiroAgentOptions(cwd='/tmp', agent_engine='v3'), transport=ScriptTransport()) as client:
            for value in (False, 0, [], '', None):
                result = await client.extension_request('_kiro/test/value', {'value':value})
                self.assertEqual(result, value)
                self.assertIs(type(result), type(value))

class ReviewVersionRegressions(unittest.TestCase):
    def test_memory_store_supports_sequential_event_loops_and_helpers(self):
        store = InMemorySessionStore()
        key = {'project_key':'project','session_id':'session'}
        asyncio.run(store.append(key, [{'type':'prompt','data':[]}]))
        self.assertEqual(len(asyncio.run(store.load(key))), 1)
        asyncio.run(store.append({**key, 'subpath':'agent'}, [{}]))
        self.assertEqual(store.size, 1)
        copy = store.get_entries(key)
        copy.clear()
        self.assertEqual(len(store.get_entries(key)), 1)
        store.clear()
        self.assertEqual(store.size, 0)

    def test_fractional_rfc3339_expiries_on_supported_python_versions(self):
        for fraction in ('5','50','1234','123456789'):
            value = {'accessToken':'synthetic','expiresAt':f'2030-01-01T00:00:00.{fraction}Z'}
            self.assertEqual(validate_client_token(value, now=datetime(2026,1,1,tzinfo=timezone.utc)), value)

@unittest.skipUnless(os.name == 'posix', 'owned process groups require POSIX')
class ReviewProcessRegressions(unittest.IsolatedAsyncioTestCase):
    def alive(self, pid):
        try:
            os.kill(pid, 0)
            stat = Path(f'/proc/{pid}/stat')
            return not stat.exists() or stat.read_text().split(') ', 1)[1].split()[0] != 'Z'
        except (ProcessLookupError, FileNotFoundError):
            return False

    async def test_device_cancel_during_cleanup_closes_process_and_fds(self):
        from kiro_agent_sdk.device_auth import DeviceLogin
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root/'kiro'
            script.write_text('#!'+sys.executable+'\nimport signal,time\n'
                'signal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint("ready", flush=True)\ntime.sleep(120)\n')
            script.chmod(0o700)
            before = len(os.listdir('/proc/self/fd')) if Path('/proc/self/fd').exists() else None
            login = DeviceLogin(str(script), root, {}, license='free', identity_provider=None, region=None, timeout=.2)
            try:
                for _ in range(200):
                    if login.status == 'failed' and login.process is not None: break
                    await asyncio.sleep(.01)
                self.assertIsNotNone(login.process)
                pid = login.process.pid
                await asyncio.wait_for(login.cancel(), 5)
                self.assertFalse(self.alive(pid))
                self.assertTrue(login.challenge.done())
                await asyncio.sleep(.05)
                if before is not None:
                    self.assertLessEqual(len(os.listdir('/proc/self/fd')), before)
            finally:
                await login.cancel()

    async def test_device_url_is_not_committed_until_its_line_is_complete(self):
        from kiro_agent_sdk.device_auth import DeviceLogin
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root/'kiro'
            script.write_text('#!'+sys.executable+'\nimport sys,time\n'
                'print("Code: ABCD-EFGH", flush=True)\n'
                'sys.stdout.write("Open this URL: https://view.awsapps.com/"); sys.stdout.flush()\n'
                'time.sleep(.2)\nprint("complete?user_code=ABCD-EFGH", flush=True)\ntime.sleep(30)\n')
            script.chmod(0o700)
            login = DeviceLogin(str(script), root, {}, license='free', identity_provider=None, region=None, timeout=5)
            try:
                await asyncio.wait_for(asyncio.shield(login.challenge), 3)
                self.assertEqual(login.details['verification_url'], 'https://view.awsapps.com/complete?user_code=ABCD-EFGH')
            finally:
                await login.cancel()

    async def test_sdk_native_hook_credentials_are_not_exported_to_cli_environment(self):
        import kiro_agent_sdk.client as client_module
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            async def hook(*args):
                seen.append(True)
                return {'block': 'policy'}
            options = KiroAgentOptions(cwd=directory, cli_path=FAKE, agent_engine='v3',
                env={'KIRO_HOME':str(Path(directory)/'kiro')},
                native_hook_bridge_mode="same-user", native_hooks={'preToolUse':[HookMatcher(hooks=[hook])]})
            with patch.object(client_module, '_spawn_cli', wraps=client_module._spawn_cli) as spawn:
                async with KiroSDKClient(options) as client:
                    environment = spawn.call_args.kwargs['env']
                    self.assertNotIn('KIRO_SDK_HOOK_TOKEN', environment)
                    self.assertNotIn('KIRO_SDK_HOOK_PORT', environment)
                    path = client._native_hook_bridge._credential_path
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    async def invoke():
                        process = await asyncio.create_subprocess_exec(*shlex.split(client._native_hook_command),
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        await process.communicate(b'{"hook_event_name":"PreToolUse"}')
                        return process.returncode
                    self.assertEqual(await invoke(), 2)
                    self.assertEqual(seen, [True])
                    path.write_bytes(path.read_bytes()+b' ')
                    self.assertEqual(await invoke(), 2)
                    self.assertEqual(seen, [True])
                self.assertFalse(path.exists())

from test_agentcore import HAS_STARLETTE, HEADER, request

@unittest.skipUnless(HAS_STARLETTE, 'requires AgentCore dependencies')
class ReviewAgentCoreRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_background_work_keeps_runtime_busy_until_completion(self):
        from kiro_agent_sdk import BackgroundTaskManager, AgentMessage, TextBlock
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        from test_background_tasks import ControlledClient
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managers = {}
            def factory(workspace, runtime_id):
                manager = BackgroundTaskManager()
                managers[runtime_id] = manager
                return manager
            async def authorize(*args): return True
            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root, cli_path=FAKE, api_key='synthetic'),
                options_factory=lambda *_: KiroAgentOptions(tools=[]), session_authorizer=authorize,
                background_tasks_factory=factory,
                background_task_options_factory=lambda *_: KiroAgentOptions(tools=[]))
            started, release = asyncio.Event(), asyncio.Event()
            class Slow(ControlledClient):
                async def receive_response(self):
                    started.set()
                    await release.wait()
                    yield AgentMessage(content=[TextBlock(text='done')])
            try:
                headers = [(HEADER,b'session-a')]
                await request(app,'POST','/invocations',b'{"prompt":"connect"}',headers)
                with patch('kiro_agent_sdk.background_tasks.KiroSDKClient', Slow):
                    status, body, _ = await request(app,'POST','/invocations',json.dumps({
                        'action':'background_task_start','request_id':'a'*32,'prompt':'job'}).encode(),headers)
                    self.assertEqual(status, 200)
                    await asyncio.wait_for(started.wait(), 3)
                    self.assertEqual(json.loads((await request(app,'GET','/ping'))[1])['status'], 'HealthyBusy')
                    release.set()
                    await managers['session-a'].wait(json.loads(body)['task_id'])
                    self.assertEqual(json.loads((await request(app,'GET','/ping'))[1])['status'], 'Healthy')
            finally:
                release.set()
                await app.state.kiro_agentcore.close()

    async def test_stream_disconnect_finishes_cleanup_without_another_request(self):
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=root,cli_path=FAKE,api_key='synthetic'),
                options_factory=lambda *_: KiroAgentOptions(tools=[]))
            incoming = [{'type':'http.request','body':b'{"prompt":"cancel","stream":true}','more_body':False}]
            started = asyncio.Event()
            async def receive():
                if incoming: return incoming.pop(0)
                await started.wait()
                await asyncio.sleep(.1)
                return {'type':'http.disconnect'}
            async def send(message):
                if message['type'] == 'http.response.start': started.set()
            scope = {'type':'http','asgi':{'version':'3.0','spec_version':'2.3'}, 'http_version':'1.1',
                'method':'POST','scheme':'http','path':'/invocations','raw_path':b'/invocations',
                'query_string':b'','root_path':'','headers':[(HEADER,b'session-a')],
                'server':('localhost',8080),'client':('localhost',1)}
            service = app.state.kiro_agentcore
            try:
                await asyncio.wait_for(app(scope,receive,send), 10)
                for _ in range(500):
                    if not service._cleanup_operations: break
                    await asyncio.sleep(.01)
                self.assertFalse(service._cleanup_operations)
                session = service.sessions['session-a']
                self.assertIsNone(session.client)
                self.assertFalse(session.lock.locked())
                self.assertEqual(service.active, 0)
            finally:
                await service.close()

    async def test_state_file_io_does_not_block_the_event_loop(self):
        import threading
        from kiro_agent_sdk.agentcore import AgentCoreConfig, create_agentcore_app
        with tempfile.TemporaryDirectory() as directory:
            service = create_agentcore_app(AgentCoreConfig(allow_unauthorized_sessions=True, state_dir=Path(directory), api_key='synthetic')).state.kiro_agentcore
            entered, release = threading.Event(), threading.Event()
            def slow():
                entered.set()
                release.wait(3)
                return 42
            operation = asyncio.create_task(service._state_io(slow))
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                await asyncio.sleep(.01)
                self.assertFalse(operation.done())
                release.set()
                self.assertEqual(await operation, 42)
            finally:
                release.set()
                await asyncio.gather(operation, return_exceptions=True)
                await service.close()

class ReviewAdditionalRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_fork_reapplies_plan_after_loading_the_saved_profile(self):
        class ForkTransport(ScriptTransport):
            async def write(self, data):
                wire=json.loads(data)
                method=wire.get('method')
                if method=='initialize':
                    result={'protocolVersion':1,'agentCapabilities':{'loadSession':True,'sessionCapabilities':{'fork':{}}}}
                elif method=='session/fork':
                    result={'sessionId':'forked'}
                elif method=='session/load':
                    result={'modes':{'currentModeId':'vibe'}}
                else:
                    if method=='session/set_mode':
                        self.queue.put_nowait({'jsonrpc':'2.0','method':'session/update','params':{
                            'sessionId':'forked','update':{'sessionUpdate':'current_mode_update','currentModeId':'plan'}}})
                    return await super().write(data)
                self.requests.append(wire)
                self.queue.put_nowait({'jsonrpc':'2.0','id':wire['id'],'result':result})
        transport=ForkTransport()
        options=KiroAgentOptions(cwd='/tmp',agent_engine='v3',resume='source',fork_session=True,permission_mode='plan')
        async with KiroSDKClient(options,transport=transport) as client:
            self.assertEqual(client._current_mode,'plan')
            methods=[r['method'] for r in transport.requests]
            self.assertLess(methods.index('session/load'),methods.index('session/set_mode'))

    async def test_nullable_optional_metadata_and_content_do_not_break_a_turn(self):
        class NullableTransport(ScriptTransport):
            async def write(self,data):
                wire=json.loads(data)
                if wire.get('method')=='session/prompt':
                    for content in (None,{'type':'text','text':'ok'}):
                        self.queue.put_nowait({'jsonrpc':'2.0','method':'session/update','params':{
                            'sessionId':'remote','update':{'sessionUpdate':'agent_message_chunk','content':content,'_meta':None}}})
                    self.queue.put_nowait({'jsonrpc':'2.0','id':wire['id'],'result':{'stopReason':'end_turn'}})
                else:
                    await super().write(data)
        async with KiroSDKClient(KiroAgentOptions(cwd='/tmp',agent_engine='v3'),transport=NullableTransport()) as client:
            await client.query('test')
            results=[message async for message in client.receive_response()]
            self.assertEqual(results[-1].result,'ok')

    async def test_execution_capacity_is_not_ignored_by_continue_policy(self):
        from kiro_agent_sdk import ProtocolError
        release=asyncio.Event()
        seen=[]
        async def policy(*args):
            seen.append(True)
            return {}
        options=KiroAgentOptions(cwd='/tmp',agent_engine='v3',hooks={
            'promptSubmit':[HookMatcher(hooks=[policy],timeout=1,on_failure='continue')]})
        async with KiroSDKClient(options,transport=ScriptTransport()) as client:
            tasks=[asyncio.create_task(release.wait()) for _ in range(64)]
            client._hook_failure_runner.tasks.update(tasks)
            try:
                with self.assertRaises(ProtocolError):
                    await client.query('must not be sent')
                self.assertFalse(seen)
                self.assertFalse(any(r.get('method')=='session/prompt' for r in client._transport.requests))
            finally:
                release.set()
                await asyncio.gather(*tasks,return_exceptions=True)
                client._hook_failure_runner.tasks.difference_update(tasks)

    async def test_sdk_hook_invalid_extras_cannot_erase_a_prompt_block(self):
        from kiro_agent_sdk import PromptBlockedError
        async def policy(*args):
            return {'block':'deny','unexpected':True}
        options=KiroAgentOptions(cwd='/tmp',agent_engine='v3',hooks={
            'promptSubmit':[HookMatcher(hooks=[policy],timeout=1,on_failure='continue')]})
        async with KiroSDKClient(options,transport=ScriptTransport()) as client:
            with self.assertRaises(PromptBlockedError): await client.query('private')
            self.assertFalse(any(r.get('method')=='session/prompt' for r in client._transport.requests))

    async def test_old_sqlite_is_rejected_before_creating_a_database(self):
        from kiro_agent_sdk.background_result_store import SQLiteBackgroundResultStore
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db'
            with patch('kiro_agent_sdk.background_result_store.sqlite3.sqlite_version_info',(3,23,0)):
                with self.assertRaisesRegex(RuntimeError,'SQLite 3.24'):
                    SQLiteBackgroundResultStore(path)
            self.assertFalse(path.exists())
