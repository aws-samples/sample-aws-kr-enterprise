import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient
from test_sdk_native_pretool import invoke_native

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


@unittest.skipUnless(os.name == "posix", "native Kiro hooks require POSIX")
class NativeHookWarningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def options(self, hook, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3", tools=[],
            env={"KIRO_HOME": str(self.root / "kiro")},
            native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native", hooks={"preToolUse": [HookMatcher(hooks=[hook])]})
        values.update(changes)
        return KiroAgentOptions(**values)

    async def invoke(self, client, session_id=None):
        return await invoke_native(client._native_hook_bridge, {
            "hook_event_name": "PreToolUse", "session_id": session_id or client.session_id,
            "tool_name": "write", "tool_input": {}})

    async def test_explicit_callback_receives_matching_warning_without_forwarding_it(self):
        seen = []
        async def hook(event, tool_id, context):
            return {'block': "policy", 'warning': "PRIVATE_WARNING"}
        async def notice(event):
            seen.append(event)
        options = self.options(hook, on_native_hook_system_message=notice)
        async with KiroSDKClient(options) as client:
            result = await self.invoke(client)
            self.assertEqual(result, {"block": True, "reason": "policy"})
            self.assertEqual(seen, [{"hook_event_name": "PreToolUse", "session_id": client.session_id,
                                     "message": "PRIVATE_WARNING"}])
            self.assertNotIn("PRIVATE_WARNING", repr(result))

    async def test_missing_or_failed_callback_fails_closed(self):
        async def hook(event, tool_id, context):
            return {'warning': "PRIVATE_WARNING"}
        async def fail(event):
            raise RuntimeError("PRIVATE_FAILURE")
        for callback in (None, fail):
            with self.subTest(callback=callback):
                async with KiroSDKClient(self.options(hook, on_native_hook_system_message=callback)) as client:
                    result = await self.invoke(client)
                    self.assertEqual(result, {"block": True, "reason": "SDK hook failed"})
                    self.assertNotIn("PRIVATE_", repr(result))

    async def test_other_session_and_callback_mutation_fail_closed(self):
        seen = []
        async def hook(event, tool_id, context):
            return {'warning': "PRIVATE_WARNING"}
        async def notice(event):
            seen.append(event)
        options = self.options(hook, on_native_hook_system_message=notice)
        async with KiroSDKClient(options) as client:
            self.assertEqual(await self.invoke(client, "other-session"),
                             {"block": True, "reason": "SDK hook failed"})
            self.assertEqual(seen, [])
            options.on_native_hook_system_message = None
            self.assertEqual(await self.invoke(client), {"block": True, "reason": "SDK hook failed"})
            options.on_native_hook_system_message = notice

    async def test_invalid_and_excessive_warnings_fail_closed(self):
        seen = []
        async def notice(event):
            seen.append(event)
        async def invalid(event, tool_id, context):
            return {'warning': "x" * 8193}
        async with KiroSDKClient(self.options(invalid, on_native_hook_system_message=notice)) as client:
            self.assertEqual(await self.invoke(client), {"block": True, "reason": "SDK hook failed"})
        self.assertEqual(seen, [])
        async def warn(event, tool_id, context):
            return {'warning': "warning"}
        options = self.options(warn, on_native_hook_system_message=notice)
        options.hooks = {"preToolUse": [HookMatcher(hooks=[warn] * 33)]}
        async with KiroSDKClient(options) as client:
            self.assertEqual(await self.invoke(client), {"block": True, "reason": "SDK hook failed"})
        self.assertEqual(len(seen), 32)

    async def test_uncooperative_callback_times_out_and_cannot_allow_tool(self):
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def hook(event, tool_id, context):
            return {'warning': "warning"}
        async def notice(event):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
        try:
            with patch("kiro_agent_sdk.client._NATIVE_HOOK_MESSAGE_TIMEOUT", 0.05):
                async with KiroSDKClient(self.options(hook, on_native_hook_system_message=notice)) as client:
                    request = asyncio.create_task(self.invoke(client))
                    await asyncio.wait_for(started.wait(), 2)
                    self.assertEqual(await asyncio.wait_for(request, 2), {"block": True, "reason": "SDK hook failed"})
                    release.set()
                    await asyncio.wait_for(finished.wait(), 2)
        finally:
            release.set()

    async def test_turn_end_cancels_pending_callback_and_fails_hook_closed(self):
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def hook(event, tool_id, context):
            return {'warning': "warning"}
        async def notice(event):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
        try:
            async with KiroSDKClient(self.options(hook, on_native_hook_system_message=notice)) as client:
                await client.query("cancel")
                request = asyncio.create_task(self.invoke(client))
                await asyncio.wait_for(started.wait(), 2)
                await client.interrupt()
                self.assertEqual(await asyncio.wait_for(request, 2), {"block": True, "reason": "SDK hook failed"})
                _ = [message async for message in client.receive_response()]
                release.set()
                await asyncio.wait_for(finished.wait(), 2)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
