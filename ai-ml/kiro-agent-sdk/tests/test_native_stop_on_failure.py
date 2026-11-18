import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import HookMatcher, KiroAgentOptions, KiroSDKClient
from kiro_agent_sdk.hooks import NativeHookBridge
from test_sdk_native_pretool import invoke_native

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


@unittest.skipUnless(os.name == "posix", "native Kiro hooks require POSIX")
class NativeStopOnFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_and_native_stop_ignore_failure_policy_block(self):
        async def fail(*args):
            raise RuntimeError("private failure")
        for path in ("sdk", "native"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as root:
                matcher = HookMatcher(hooks=[fail], timeout=1, on_failure="block")
                extra = ({"native_hooks": {"agentStop": [matcher]}} if path == "native" else
                         {"hooks": {"agentStop": [matcher]}, "sdk_stop_hook_mode": "native"})
                options = KiroAgentOptions(native_hook_bridge_mode="same-user",
                                       cwd=root, cli_path=FAKE, agent_engine="v3", tools=[],
                                       env={"KIRO_HOME": str(Path(root) / "kiro")}, **extra)
                async with KiroSDKClient(options) as client:
                    await client.query("cancel")
                    response = await invoke_native(client._native_hook_bridge, {
                        "hook_event_name": "Stop", "session_id": client.session_id})
                    self.assertEqual(response, {"block": False})
                    self.assertEqual(client._native_stop_blocks, 0)
                    await client.interrupt()
                    _ = [m async for m in client.receive_response()]

    async def test_valid_block_is_preserved_and_invalid_warning_is_discarded(self):
        warnings = []
        async def block(*args):
            return {'continue': "continue work", 'warning': "warning"}
        async def notice(event):
            warnings.append(event)
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[], env={"KIRO_HOME": str(Path(root) / "kiro")},
                native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native", on_native_hook_system_message=notice,
                hooks={"agentStop": [HookMatcher(hooks=[block], timeout=1, on_failure="continue")]})
            async with KiroSDKClient(options) as client:
                await client.query("cancel")
                response = await invoke_native(client._native_hook_bridge, {
                    "hook_event_name": "Stop", "session_id": client.session_id})
                self.assertEqual(response, {"block": True, "reason": "continue work"})
                self.assertEqual([item["message"] for item in warnings], ["warning"])
                await client.interrupt()
                _ = [m async for m in client.receive_response()]
        warnings.clear()
        async def invalid(*args):
            return {"systemMessage": "discard", "decision": "invalid"}
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[], env={"KIRO_HOME": str(Path(root) / "kiro")},
                native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native", on_native_hook_system_message=notice,
                hooks={"agentStop": [HookMatcher(hooks=[invalid], timeout=1, on_failure="continue")]})
            async with KiroSDKClient(options) as client:
                await client.query("cancel")
                response = await invoke_native(client._native_hook_bridge, {
                    "hook_event_name": "Stop", "session_id": client.session_id})
                self.assertEqual(response, {"block": False})
                self.assertEqual(warnings, [])
                await client.interrupt()
                _ = [m async for m in client.receive_response()]

    async def test_native_stop_requires_bounded_hooks(self):
        async def hook(*args):
            return {}
        for matchers in ([HookMatcher(hooks=[hook], on_failure="continue")],
                         [HookMatcher(hooks=[hook], timeout=16, on_failure="block")],
                         [HookMatcher(hooks=[hook] * 5, timeout=1, on_failure="continue")],
                         [HookMatcher(hooks=[hook] * 4, timeout=4, on_failure="continue")],
                         [HookMatcher(hooks=[hook] * 4, timeout=3.251, on_failure="continue")],
                         [HookMatcher(hooks=[hook], timeout=1, on_failure="continue"),
                          HookMatcher(hooks=[hook])]):
            with self.subTest(matchers=len(matchers)), self.assertRaises(ValueError):
                NativeHookBridge({"Stop": matchers})
        NativeHookBridge({"Stop": [HookMatcher(hooks=[hook] * 4, timeout=3.25, on_failure="continue")]})

    async def test_multiple_sdk_and_native_hooks_continue_and_preserve_block(self):
        for path in ("sdk", "native"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as root:
                calls, warnings = [], []

                async def fail(*args):
                    calls.append("failure")
                    raise RuntimeError("private failure")

                async def invalid(*args):
                    calls.append("invalid")
                    return {"systemMessage": "discard", "decision": "invalid"}

                async def block(*args):
                    calls.append("block")
                    return {'warning': "warning", 'continue': "continue"}

                async def notice(event):
                    warnings.append(event)

                matcher = HookMatcher(hooks=[fail, invalid, block], timeout=1, on_failure="block")
                extra = ({"native_hooks": {"agentStop": [matcher]}} if path == "native" else
                         {"hooks": {"agentStop": [matcher]}, "sdk_stop_hook_mode": "native"})
                options = KiroAgentOptions(native_hook_bridge_mode="same-user",
                                       cwd=root, cli_path=FAKE, agent_engine="v3", tools=[],
                                       env={"KIRO_HOME": str(Path(root) / "kiro")},
                                       on_native_hook_system_message=notice, **extra)
                async with KiroSDKClient(options) as client:
                    await client.query("cancel")
                    response = await invoke_native(client._native_hook_bridge, {
                        "hook_event_name": "Stop", "session_id": client.session_id})
                    self.assertEqual(response, {"block": True, "reason": "continue"})
                    self.assertEqual(calls, ["failure", "invalid", "block"])
                    self.assertEqual([event["message"] for event in warnings], ["warning"])
                    await client.interrupt()
                    _ = [message async for message in client.receive_response()]

    async def test_multiple_sdk_hooks_recheck_authorization_before_next_callback(self):
        started, release = asyncio.Event(), asyncio.Event()
        permitted = True
        calls = []

        async def first(*args):
            calls.append("first")
            started.set()
            await release.wait()
            return {}

        async def second(*args):
            calls.append("second")
            return {'warning': "private"}

        async def guard():
            return permitted

        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[],
                env={"KIRO_HOME": str(Path(root) / "kiro")}, native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                hooks={"agentStop": [HookMatcher(hooks=[first, second], timeout=1, on_failure="continue")]})
            async with KiroSDKClient(options) as client:
                client._interaction_guard = guard
                await client.query("cancel")
                task = asyncio.create_task(invoke_native(client._native_hook_bridge, {
                    "hook_event_name": "Stop", "session_id": client.session_id}))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    permitted = False
                    release.set()
                    response = await asyncio.wait_for(task, 3)
                    self.assertFalse(response["block"])
                    self.assertEqual(calls, ["first"])
                    self.assertNotIn("private", str(response))
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_multiple_sdk_hooks_discard_late_timed_out_decisions(self):
        release, finished = asyncio.Event(), asyncio.Event()
        calls = []

        async def late(*args):
            calls.append("late")
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'continue': "late"}

        async def next_hook(*args):
            calls.append("next")
            return {}

        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[],
                env={"KIRO_HOME": str(Path(root) / "kiro")}, native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                hooks={"agentStop": [HookMatcher(hooks=[late, next_hook], timeout=0.05, on_failure="continue")]})
            try:
                async with KiroSDKClient(options) as client:
                    await client.query("cancel")
                    response = await invoke_native(client._native_hook_bridge, {
                        "hook_event_name": "Stop", "session_id": client.session_id})
                    self.assertEqual(response, {"block": False})
                    self.assertEqual(calls, ["late", "next"])
                    release.set()
                    await asyncio.wait_for(finished.wait(), 2)
                    self.assertEqual(client._native_stop_blocks, 0)
                    await client.interrupt()
                    _ = [message async for message in client.receive_response()]
            finally:
                release.set()

    async def test_interrupt_prevents_following_stop_hook(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def first(*args):
            calls.append("first")
            started.set()
            await release.wait()
            return {}

        async def second(*args):
            calls.append("second")
            return {}

        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[],
                env={"KIRO_HOME": str(Path(root) / "kiro")}, native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                hooks={"agentStop": [HookMatcher(hooks=[first, second], timeout=1, on_failure="continue")]})
            try:
                async with KiroSDKClient(options) as client:
                    await client.query("cancel")
                    task = asyncio.create_task(invoke_native(client._native_hook_bridge, {
                        "hook_event_name": "Stop", "session_id": client.session_id}))
                    await asyncio.wait_for(started.wait(), 2)
                    await client.interrupt()
                    release.set()
                    response = await asyncio.wait_for(task, 3)
                    self.assertFalse(response["block"])
                    self.assertEqual(calls, ["first"])
                    _ = [message async for message in client.receive_response()]
            finally:
                release.set()

    async def test_multiple_hooks_do_not_continue_after_warning_timeout(self):
        release = asyncio.Event()
        calls = []

        async def first(*args):
            calls.append("first")
            return {'warning': "private warning"}

        async def second(*args):
            calls.append("second")
            return {}

        async def notice(event):
            await release.wait()

        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[],
                env={"KIRO_HOME": str(Path(root) / "kiro")}, native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                on_native_hook_system_message=notice,
                hooks={"agentStop": [HookMatcher(hooks=[first, second], timeout=1, on_failure="continue")]})
            try:
                async with KiroSDKClient(options) as client:
                    await client.query("cancel")
                    response = await asyncio.wait_for(invoke_native(client._native_hook_bridge, {
                        "hook_event_name": "Stop", "session_id": client.session_id}), 3)
                    self.assertFalse(response["block"])
                    self.assertEqual(calls, ["first"])
                    self.assertNotIn("private", str(response))
            finally:
                release.set()

    async def test_multiple_hooks_fail_closed_on_stalled_reauthorization(self):
        release = asyncio.Event()
        calls = []
        guard_calls = 0

        async def first(*args):
            calls.append("first")
            return {}

        async def second(*args):
            calls.append("second")
            return {}

        async def guard():
            nonlocal guard_calls
            guard_calls += 1
            if guard_calls > 1:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            return True

        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[],
                env={"KIRO_HOME": str(Path(root) / "kiro")}, native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                hooks={"agentStop": [HookMatcher(hooks=[first, second], timeout=1, on_failure="continue")]})
            try:
                async with KiroSDKClient(options) as client:
                    client._interaction_guard = guard
                    await client.query("cancel")
                    response = await asyncio.wait_for(invoke_native(client._native_hook_bridge, {
                        "hook_event_name": "Stop", "session_id": client.session_id}), 3)
                    self.assertFalse(response["block"])
                    self.assertEqual(calls, ["first"])
                    release.set()
                    await asyncio.sleep(0)
                    self.assertEqual(calls, ["first"])
            finally:
                release.set()

    async def test_multiple_sdk_hooks_do_not_ignore_strict_callback_failure(self):
        calls = []

        async def continued(*args):
            calls.append("continued")
            raise RuntimeError("discarded failure")

        async def strict(*args):
            calls.append("strict")
            raise RuntimeError("private failure")

        async def later(*args):
            calls.append("later")
            return {}

        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[],
                env={"KIRO_HOME": str(Path(root) / "kiro")}, native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                hooks={"agentStop": [HookMatcher(hooks=[continued], timeout=1, on_failure="continue"),
                                HookMatcher(hooks=[strict, later], timeout=1)]})
            async with KiroSDKClient(options) as client:
                await client.query("cancel")
                response = await invoke_native(client._native_hook_bridge, {
                    "hook_event_name": "Stop", "session_id": client.session_id})
                self.assertFalse(response["block"])
                self.assertEqual(calls, ["continued", "strict"])
                self.assertNotIn("private", str(response))

    async def test_raw_native_stop_preserves_valid_block(self):
        async def block(*args):
            return {'continue': "continue work"}
        bridge = NativeHookBridge({"Stop": [HookMatcher(hooks=[block], timeout=1, on_failure="block")]})
        await bridge.start()
        try:
            response = await invoke_native(bridge, {"hook_event_name": "Stop"})
            self.assertEqual(response, {"block": True, "reason": "continue work"})
        finally:
            await bridge.close()

    async def test_warning_callback_failure_is_not_treated_as_hook_continue(self):
        async def hook(*args):
            return {'warning': "warning"}
        async def notice(event):
            raise RuntimeError("private failure")
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[], env={"KIRO_HOME": str(Path(root) / "kiro")},
                native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native", on_native_hook_system_message=notice,
                hooks={"agentStop": [HookMatcher(hooks=[hook], timeout=1, on_failure="continue")]})
            async with KiroSDKClient(options) as client:
                await client.query("cancel")
                response = await invoke_native(client._native_hook_bridge, {
                    "hook_event_name": "Stop", "session_id": client.session_id})
                self.assertFalse(response["block"])
                self.assertNotIn("private failure", str(response))

    async def test_uncooperative_timeout_does_not_later_block(self):
        release, finished = asyncio.Event(), asyncio.Event()
        async def late(*args):
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'continue': "late"}
        with tempfile.TemporaryDirectory() as root:
            options = KiroAgentOptions(
                cwd=root, cli_path=FAKE, agent_engine="v3", tools=[], env={"KIRO_HOME": str(Path(root) / "kiro")},
                native_hook_bridge_mode="same-user", sdk_stop_hook_mode="native",
                hooks={"agentStop": [HookMatcher(hooks=[late], timeout=0.05, on_failure="block")]})
            try:
                async with KiroSDKClient(options) as client:
                    await client.query("cancel")
                    response = await invoke_native(client._native_hook_bridge, {
                        "hook_event_name": "Stop", "session_id": client.session_id})
                    self.assertEqual(response, {"block": False})
                    self.assertEqual(client._native_stop_blocks, 0)
                    release.set()
                    await asyncio.wait_for(finished.wait(), 3)
                    self.assertEqual(client._native_stop_blocks, 0)
                    await client.interrupt()
                    _ = [m async for m in client.receive_response()]
            finally:
                release.set()

