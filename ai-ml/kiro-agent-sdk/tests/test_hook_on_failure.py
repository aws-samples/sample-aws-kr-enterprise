import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import (HookMatcher, KiroAgentOptions, KiroSDKClient, PermissionResultAllow,
                            PromptBlockedError, ProtocolError)
from kiro_agent_sdk.hooks import HOOK_TRIGGERS, NativeHookBridge

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class HookOnFailureTests(unittest.IsolatedAsyncioTestCase):
    def options(self, event, hook, policy, **values):
        trigger = next(name for name, value in HOOK_TRIGGERS.items() if value == event)
        return KiroAgentOptions(cli_path=FAKE, agent_engine="v3", tools=[],
                                hooks={trigger: [HookMatcher(hooks=[hook], on_failure=policy)]}, **values)

    async def test_prompt_failure_blocks_or_continues_when_requested(self):
        async def fail(event, tool_id, context):
            raise RuntimeError("secret from hook")
        async with KiroSDKClient(self.options("UserPromptSubmit", fail, "block")) as client:
            with self.assertRaises(PromptBlockedError) as caught:
                await client.query("hello")
            self.assertNotIn("secret from hook", str(caught.exception))
        async with KiroSDKClient(self.options("UserPromptSubmit", fail, "continue")) as client:
            await client.query("hello")
            self.assertTrue([message async for message in client.receive_response()])

    async def test_permission_failures_block_before_can_use_tool_or_continue(self):
        for event in ("PreToolUse",):
            for policy in ("block", "continue"):
                with self.subTest(event=event, policy=policy):
                    calls = []
                    async def fail(payload, tool_id, context):
                        return None
                    async def allow(name, data, context):
                        calls.append(name)
                        return PermissionResultAllow()
                    opts = self.options(event, fail, policy, can_use_tool=allow)
                    async with KiroSDKClient(opts) as client:
                        await client.query("permission")
                        self.assertEqual([m async for m in client.receive_response()][-1].result,
                                         "no" if policy == "block" else "yes")
                    self.assertEqual(calls, [] if policy == "block" else ["write"])

    async def test_invalid_output_is_transactionally_discarded(self):
        async def invalid(payload, tool_id, context):
            return {'context': "discard this", 'title': ""}
        for policy in ("block", "continue"):
            with self.subTest(policy=policy):
                client = KiroSDKClient(self.options("UserPromptSubmit", invalid, policy))
                contexts, titles = [], []
                blocked = await client._run_hooks("UserPromptSubmit", {"prompt": "hi"},
                                                  additional_context=contexts, session_titles=titles)
                self.assertEqual((contexts, titles), ([], []))
                self.assertEqual(blocked, policy == "block")

    async def test_invalid_permission_output_does_not_leave_an_allow(self):
        async def invalid(payload, tool_id, context):
            return {"allow": "yes"}
        for policy in ("block", "continue"):
            client = KiroSDKClient(self.options("PreToolUse", invalid, policy))
            decisions = []
            blocked = await client._run_hooks("PreToolUse", {"tool_name": "write"},
                                              permission_decisions=decisions)
            self.assertEqual(decisions, [])
            self.assertEqual(blocked, policy == "block")

    async def test_unknown_top_level_keys_are_ignored_for_explicit_policy(self):
        async def hook(payload, tool_id, context):
            return {"unknown": "ignored"}
        client = KiroSDKClient(self.options("UserPromptSubmit", hook, "block"))
        self.assertFalse(await client._run_hooks("UserPromptSubmit", {"prompt": "hi"}))

    async def test_observed_post_hooks_continue_and_stop_events_ignore_block(self):
        async def fail(payload, tool_id, context):
            raise RuntimeError("hook failed")
        for event, policy in (("PostToolUse", "continue"), ("Stop", "continue"), ("Stop", "block")):
            with self.subTest(event=event, policy=policy):
                client = KiroSDKClient(self.options(event, fail, policy))
                self.assertFalse(await client._run_hooks(event, {"tool_name": "write"}))

    async def test_timeout_does_not_wait_for_uncooperative_hook_or_use_late_result(self):
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def late(payload, tool_id, context):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'block': True}
        opts = self.options("UserPromptSubmit", late, "continue")
        opts.hooks["promptSubmit"][0].timeout = 0.05
        client = KiroSDKClient(opts)
        try:
            result = await asyncio.wait_for(client._run_hooks("UserPromptSubmit", {"prompt": "hi"}), 2)
            self.assertTrue(started.is_set())
            self.assertFalse(result)
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
        finally:
            release.set()

    async def test_changing_policy_during_callback_fails_closed(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def hook(payload, tool_id, context):
            started.set()
            await release.wait()
            return {}
        opts = self.options("UserPromptSubmit", hook, "block")
        client = KiroSDKClient(opts)
        task = asyncio.create_task(client._run_hooks("UserPromptSubmit", {"prompt": "hi"}))
        try:
            await started.wait()
            opts.hooks["promptSubmit"][0].on_failure = "continue"
            release.set()
            with self.assertRaises(ValueError):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_changing_policy_during_permission_callback_cannot_allow(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def hook(payload, tool_id, context):
            return {}
        async def allow(name, data, context):
            started.set()
            await release.wait()
            return PermissionResultAllow()
        opts = self.options("PreToolUse", hook, "block", can_use_tool=allow)
        try:
            async with KiroSDKClient(opts) as client:
                await client.query("permission")
                await asyncio.wait_for(started.wait(), 3)
                opts.hooks["preToolUse"][0].on_failure = "continue"
                release.set()
                with self.assertRaises(ProtocolError):
                    _ = [m async for m in client.receive_response()]
        finally:
            release.set()

    async def test_changing_policy_during_prompt_authorization_prevents_send(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def hook(payload, tool_id, context):
            return {}
        async def guard():
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
                await release.wait()
            return True
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "requests.jsonl"
            opts = self.options("UserPromptSubmit", hook, "block", env={"FAKE_LOG_REQUESTS": str(log)})
            pending = None
            try:
                async with KiroSDKClient(opts) as client:
                    client._prompt_send_guard = guard
                    pending = asyncio.create_task(client.query("hello"))
                    await asyncio.wait_for(started.wait(), 3)
                    opts.hooks["promptSubmit"][0].on_failure = "continue"
                    release.set()
                    with self.assertRaises(ValueError):
                        await pending
                messages = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertFalse(any(message.get("method") == "session/prompt" for message in messages))
            finally:
                release.set()
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)


class McpHookOnFailureTests(unittest.IsolatedAsyncioTestCase):
    pass

class NativeHookOnFailureTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, bridge):
        command, environment = await bridge.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", int(environment["KIRO_SDK_HOOK_PORT"]))
        writer.write((environment["KIRO_SDK_HOOK_TOKEN"] + "\n" + json.dumps({
            "hook_event_name": "PreToolUse", "tool_name": "write"}) + "\n").encode())
        await writer.drain()
        try:
            return json.loads(await asyncio.wait_for(reader.readline(), 3))
        finally:
            writer.close()
            await writer.wait_closed()
            self.addAsyncCleanup(bridge.close)
            if any(not task.done() for task in bridge._failure_runner.tasks):
                with self.assertRaisesRegex(Exception, "cleaning up|cleanup"):
                    await bridge.close()
            else:
                await bridge.close()

    async def test_native_pretool_failure_blocks_and_unknown_keys_are_ignored(self):
        async def fail(*args):
            raise RuntimeError("private failure")
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[fail], on_failure="block")]})
        response = await self.call(bridge)
        self.assertTrue(response["block"])
        self.assertNotIn("private failure", str(response))
        async def no_op(*args):
            return {"unknown": "ignored"}
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[no_op], on_failure="block")]})
        self.assertFalse((await self.call(bridge))["block"])

    async def test_native_pretool_continue_ignores_failures_but_preserves_explicit_deny(self):
        async def fail(*args):
            raise RuntimeError("private failure")
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[fail], timeout=1, on_failure="continue")]})
        self.assertFalse((await self.call(bridge))["block"])
        async def invalid(*args):
            return {"hookSpecificOutput": {"permissionDecision": "unexpected"}}
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[invalid], timeout=1, on_failure="continue")]})
        self.assertFalse((await self.call(bridge))["block"])
        messages = []
        async def message(*args):
            return {'warning': "discard"}
        async def deliver(*args):
            messages.append(args)
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[message], timeout=1, on_failure="continue")]},
                                  on_system_message=deliver)
        self.assertFalse((await self.call(bridge))["block"])
        self.assertEqual(len(messages), 1)
        messages.clear()
        async def invalid_warning(*args):
            return {"systemMessage": "discard",
                    "hookSpecificOutput": {"permissionDecision": "unexpected"}}
        bridge = NativeHookBridge({"PreToolUse": [
            HookMatcher(hooks=[invalid_warning], timeout=1, on_failure="continue")]},
            on_system_message=deliver)
        self.assertFalse((await self.call(bridge))["block"])
        self.assertEqual(messages, [])
        async def deny(*args):
            return {'block': "explicit"}
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[deny], timeout=1, on_failure="continue")]})
        self.assertTrue((await self.call(bridge))["block"])

    async def test_native_pretool_continue_discards_uncooperative_timeout(self):
        release, finished = asyncio.Event(), asyncio.Event()
        async def late(*args):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'block': True}
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[late], timeout=0.05, on_failure="continue")]})
        try:
            self.assertFalse((await self.call(bridge))["block"])
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
        finally:
            release.set()

    async def test_native_pretool_continue_requires_bounded_hooks(self):
        async def hook(*args):
            return {}
        for matchers in ([HookMatcher(hooks=[hook], on_failure="continue")],
                         [HookMatcher(hooks=[hook], timeout=31, on_failure="continue")],
                         [HookMatcher(hooks=[hook] * 9, timeout=0.1, on_failure="continue")],
                         [HookMatcher(hooks=[hook] * 2, timeout=16, on_failure="continue")],
                         [HookMatcher(hooks=[hook], timeout=1, on_failure="continue"),
                          HookMatcher(hooks=[hook])]):
            with self.subTest(count=len(matchers)), self.assertRaises(ValueError):
                NativeHookBridge({"PreToolUse": matchers})

    async def test_native_pretool_multiple_hooks_continue_in_order_and_preserve_deny(self):
        seen, warnings = [], []
        async def fail(*args):
            seen.append("fail")
            raise RuntimeError("private")
        async def invalid(*args):
            seen.append("invalid")
            return {"systemMessage": "discard", "hookSpecificOutput": {"permissionDecision": "invalid"}}
        async def deny(*args):
            seen.append("deny")
            return {'block': "protected"}
        async def never(*args):
            seen.append("never")
            return {}
        async def warning(*args):
            warnings.append(args)
        bridge = NativeHookBridge({"PreToolUse": [
            HookMatcher(hooks=[fail, invalid], timeout=1, on_failure="continue"),
            HookMatcher(hooks=[deny, never], timeout=1, on_failure="block")]}, on_system_message=warning)
        self.assertEqual(await self.call(bridge), {"block": True, "reason": "protected"})
        self.assertEqual(seen, ["fail", "invalid", "deny"])
        self.assertEqual(warnings, [])

    async def test_native_pretool_multiple_hooks_strict_failure_and_late_result(self):
        release, finished = asyncio.Event(), asyncio.Event()
        seen = []
        async def late(*args):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'block': "late"}
        async def later(*args):
            seen.append("later")
            return {}
        async def strict(*args):
            raise RuntimeError("private")
        try:
            bridge = NativeHookBridge({"PreToolUse": [
                HookMatcher(hooks=[late], timeout=0.05, on_failure="continue"),
                HookMatcher(hooks=[later], timeout=1)]})
            self.assertFalse((await self.call(bridge))["block"])
            self.assertEqual(seen, ["later"])
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
            bridge = NativeHookBridge({"PreToolUse": [
                HookMatcher(hooks=[later], timeout=1, on_failure="continue"),
                HookMatcher(hooks=[strict], timeout=1)]})
            response = await self.call(bridge)
            self.assertTrue(response["block"])
            self.assertNotIn("private", str(response))
        finally:
            release.set()

    async def test_native_pretool_multiple_hooks_recheck_authorization(self):
        allowed = True
        seen = []
        async def revoke(*args):
            nonlocal allowed
            seen.append("revoke")
            allowed = False
            return {}
        async def later(*args):
            seen.append("later")
            return {}
        async def guard():
            return allowed
        bridge = NativeHookBridge({"PreToolUse": [
            HookMatcher(hooks=[revoke, later], timeout=1, on_failure="continue")]},
            failure_guard_factory=lambda: guard)
        self.assertTrue((await self.call(bridge))["block"])
        self.assertEqual(seen, ["revoke"])

    async def test_native_pretool_multiple_hooks_warning_timeout_blocks(self):
        release, finished = asyncio.Event(), asyncio.Event()
        seen = []
        async def first(*args):
            return {'warning': "warning"}
        async def second(*args):
            seen.append("second")
            return {}
        async def warning(*args):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
        bridge = NativeHookBridge({"PreToolUse": [
            HookMatcher(hooks=[first, second], timeout=1, on_failure="continue")]},
            on_system_message=warning)
        try:
            self.assertTrue((await self.call(bridge))["block"])
            self.assertEqual(seen, [])
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
        finally:
            release.set()

    async def test_native_pretool_multiple_hooks_reject_mutation(self):
        seen = []
        async def first(*args):
            matcher.timeout = 2
            return {}
        async def second(*args):
            seen.append("second")
            return {}
        matcher = HookMatcher(hooks=[first, second], timeout=1, on_failure="continue")
        bridge = NativeHookBridge({"PreToolUse": [matcher]})
        self.assertTrue((await self.call(bridge))["block"])
        self.assertEqual(seen, [])

    async def test_native_pretool_timeout_does_not_wait_for_late_allow(self):
        release, finished = asyncio.Event(), asyncio.Event()
        async def late(*args):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {}
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[late], timeout=0.05, on_failure="block")]})
        try:
            self.assertTrue((await self.call(bridge))["block"])
            release.set()
            await asyncio.wait_for(finished.wait(), 2)
        finally:
            release.set()

    async def test_native_pretool_guard_rejects_after_authorization_changes(self):
        started, release = asyncio.Event(), asyncio.Event()
        permitted = True
        async def hook(*args):
            started.set()
            await release.wait()
            return {}
        def factory():
            async def guard():
                if not permitted:
                    raise PermissionError("authorization changed")
                return True
            return guard
        bridge = NativeHookBridge({"PreToolUse": [HookMatcher(hooks=[hook], on_failure="block")]},
                                  failure_guard_factory=factory)
        pending = asyncio.create_task(self.call(bridge))
        try:
            await started.wait()
            permitted = False
            release.set()
            self.assertTrue((await asyncio.wait_for(pending, 3))["block"])
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    @unittest.skipUnless(os.name == "posix", "native Kiro hooks require POSIX")
    async def test_native_policy_is_preserved_for_sdk_and_native_configuration(self):
        async def fail(*args):
            raise RuntimeError("private failure")
        for path in ("sdk", "native"):
            for policy in ("block", "continue"):
                with self.subTest(path=path, policy=policy), tempfile.TemporaryDirectory() as root:
                    matcher = HookMatcher(hooks=[fail], timeout=1, on_failure=policy)
                    extra = ({"hooks": {"preToolUse": [matcher]}, "sdk_pretool_hook_mode": "native"}
                             if path == "sdk" else {"native_hooks": {"preToolUse": [matcher]}})
                    opts = KiroAgentOptions(native_hook_bridge_mode="same-user",
                                            cli_path=FAKE, agent_engine="v3", tools=[],
                                            env={"KIRO_HOME": root}, **extra)
                    async with KiroSDKClient(opts) as client:
                        bridge = client._native_hook_bridge
                        reader, writer = await asyncio.open_connection(
                            "127.0.0.1", bridge.server.sockets[0].getsockname()[1])
                        writer.write((bridge.token + "\n" + json.dumps({
                            "hook_event_name": "PreToolUse", "tool_name": "write",
                            "session_id": client.session_id}) + "\n").encode())
                        await writer.drain()
                        try:
                            self.assertEqual(json.loads(await asyncio.wait_for(reader.readline(), 3))["block"],
                                             policy == "block")
                        finally:
                            writer.close()
                            await writer.wait_closed()

    async def test_interrupted_turn_cannot_receive_late_native_continue(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def hook(*args):
            started.set()
            await release.wait()
            return {}
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cli_path=FAKE, agent_engine="v3", tools=[], env={"KIRO_HOME": root},
                native_hook_bridge_mode="same-user", sdk_pretool_hook_mode="native",
                hooks={"preToolUse": [HookMatcher(hooks=[hook], timeout=2, on_failure="continue")]})
            pending = None
            writer = None
            try:
                async with KiroSDKClient(options) as client:
                    await client.query("cancel")
                    bridge = client._native_hook_bridge
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", bridge.server.sockets[0].getsockname()[1])
                    writer.write((bridge.token + "\n" + json.dumps({
                        "hook_event_name": "PreToolUse", "tool_name": "write",
                        "session_id": client.session_id}) + "\n").encode())
                    await writer.drain()
                    pending = asyncio.create_task(reader.readline())
                    await asyncio.wait_for(started.wait(), 3)
                    await client.interrupt()
                    _ = [m async for m in client.receive_response()]
                    release.set()
                    self.assertTrue(json.loads(await asyncio.wait_for(pending, 3))["block"])
            finally:
                release.set()
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)


