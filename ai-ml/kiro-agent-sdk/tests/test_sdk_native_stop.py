import asyncio
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError
from test_sdk_native_pretool import invoke_native

FAKE = str(Path(__file__).with_name('fake_kiro.py'))


class NativeSdkStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def options(self, hooks, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine='v3', tools=[],
            env={'KIRO_HOME': str(self.root / 'kiro')}, hooks={'agentStop': hooks}, native_hook_bridge_mode="same-user", sdk_stop_hook_mode='native')
        values.update(changes)
        return KiroAgentOptions(**values)

    async def invoke(self, client, **extra):
        payload = {'hook_event_name': 'Stop', 'session_id': client.session_id, 'cwd': str(self.root), **extra}
        return await invoke_native(client._native_hook_bridge, payload)

    async def test_block_is_bounded_and_hook_is_not_repeated_at_receive(self):
        events = []
        warnings = []
        async def hook(event, tool_id, context):
            events.append((event['hook_event_name'], event['continued'], tool_id))
            return {'continue': 'CONTINUE_MARKER', 'warning': 'STOP_WARNING'}
        async def notice(event):
            warnings.append(event)
        async with KiroSDKClient(self.options([HookMatcher(hooks=[hook])],
                                               on_native_hook_system_message=notice)) as client:
            await client.query('cancel')
            first = await self.invoke(client)
            second = await self.invoke(client)
            self.assertEqual(first, {'block': True, 'reason': 'CONTINUE_MARKER'})
            self.assertEqual(second, {'block': False})
            await client.interrupt()
            _ = [item async for item in client.receive_response()]
            self.assertEqual(events, [('Stop', False, None)])
            await client.query('cancel')
            self.assertTrue((await self.invoke(client))['block'])
            await client.interrupt()
            _ = [item async for item in client.receive_response()]
        self.assertEqual(events, [('Stop', False, None), ('Stop', False, None)])
        self.assertEqual([item['message'] for item in warnings], ['STOP_WARNING', 'STOP_WARNING'])

    async def test_warning_count_is_bounded_across_native_stop_events(self):
        seen = []
        async def hook(event, tool_id, context):
            return {'warning': 'warning'}
        async def notice(event):
            seen.append(event)
        options = self.options([HookMatcher(hooks=[hook])], on_native_hook_system_message=notice)
        async with KiroSDKClient(options) as client:
            await client.query('cancel')
            for _ in range(32):
                self.assertEqual(await self.invoke(client), {'block': False})
            self.assertEqual(await self.invoke(client), {'block': False, 'reason': 'SDK hook failed', 'warning': 'SDK hook failed'})
            self.assertEqual(len(seen), 32)
            await client.interrupt()
            with self.assertRaisesRegex(ProtocolError, 'Native Stop hook failed'):
                _ = [item async for item in client.receive_response()]

    async def test_active_flag_tracks_only_prior_sdk_blocks(self):
        seen = []
        async def hook(event, tool_id, context):
            seen.append((event['continued'], event['kiro_observed_continued']))
            return {'continue': 'continue'}
        async with KiroSDKClient(self.options([HookMatcher(hooks=[hook])], max_native_stop_blocks=2)) as client:
            await client.query('cancel')
            self.assertTrue((await self.invoke(client))['block'])
            self.assertTrue((await self.invoke(client))['block'])
            self.assertFalse((await self.invoke(client))['block'])
            await client.interrupt()
            _ = [item async for item in client.receive_response()]
        self.assertEqual(seen, [(False, True), (True, True)])

    async def test_hook_mutation_cannot_cause_unbounded_failure_blocks(self):
        options = None
        async def hook(event, tool_id, context):
            options.max_native_stop_blocks = 16
            return {'continue': 'unsafe'}
        options = self.options([HookMatcher(hooks=[hook])])
        async with KiroSDKClient(options) as client:
            await client.query('cancel')
            self.assertFalse((await self.invoke(client))['block'])
            self.assertFalse((await self.invoke(client))['block'])
            options.max_native_stop_blocks = 1
            await client.interrupt()
            with self.assertRaisesRegex(ProtocolError, 'Native Stop hook failed'):
                _ = [item async for item in client.receive_response()]

    async def test_other_sessions_and_reported_subagents_are_not_controlled(self):
        called = []
        async def hook(event, tool_id, context):
            called.append(event)
            return {'continue': True}
        async with KiroSDKClient(self.options([HookMatcher(hooks=[hook])])) as client:
            await client.query('cancel')
            self.assertEqual(await self.invoke(client, session_id='other'), {'block': False})
            self.assertEqual(await self.invoke(client, _meta={'kiro': {'agentSubtaskId': 'child'}}),
                             {'block': False})
            await client.interrupt()
            _ = [item async for item in client.receive_response()]
        self.assertEqual(called, [])

    async def test_uncooperative_timeout_is_bounded_and_late_value_cannot_block_again(self):
        release, finished = asyncio.Event(), asyncio.Event()
        async def hook(event, tool_id, context):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'continue': 'LATE'}
        async with KiroSDKClient(self.options([HookMatcher(hooks=[hook], timeout=0.05)])) as client:
            await client.query('cancel')
            response = await asyncio.wait_for(self.invoke(client), 2)
            self.assertFalse(response['block'])
            self.assertNotIn('LATE', str(response))
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            self.assertEqual(await self.invoke(client), {'block': False})
            await client.interrupt()
            with self.assertRaisesRegex(ProtocolError, 'Native Stop hook failed'):
                _ = [item async for item in client.receive_response()]
