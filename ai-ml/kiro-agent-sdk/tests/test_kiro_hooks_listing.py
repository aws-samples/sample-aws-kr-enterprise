import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import CLIConnectionError, HookMatcher, KiroAgentOptions, KiroSDKClient, ProtocolError
from test_transport import FakeTransport

FAKE = str(Path(__file__).with_name("fake_kiro.py"))


class KiroHookListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "requests.jsonl"

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def options(self, **changes):
        values = dict(cwd=self.root, cli_path=FAKE, agent_engine="v3",
            env={"KIRO_HOME": str(self.root / "kiro"), "FAKE_HOOK_EVENTS": "1",
                 "FAKE_LOG_REQUESTS": str(self.log)})
        values.update(changes)
        return KiroAgentOptions(**values)

    def listings(self):
        return [item["params"] for item in map(json.loads, self.log.read_text().splitlines())
                if item.get("method") == "_kiro/hooks/list"]

    async def test_session_scoped_filters_and_unmodified_hook_guards(self):
        async def hook(event, tool_id, context):
            return {}
        options = self.options(native_hook_bridge_mode="same-user", native_hooks={"preToolUse": [HookMatcher(hooks=[hook])]})
        async with KiroSDKClient(options) as client:
            before = copy.deepcopy(client._hook_snapshots_by_session)
            empty = await client.list_kiro_hooks(trigger="Stop")
            self.assertEqual(empty["hooks"], [])
            self.assertEqual(client._hook_snapshots_by_session, before)
            result = await client.list_kiro_hooks(trigger="PreToolUse", tool_id="read",
                                                  tool_tags=["fs", "inspect"], include_disabled=True)
            self.assertTrue(result["hooks"])
            self.assertEqual(self.listings()[-1], {"sessionId": client.session_id,
                "includeDisabled": True, "trigger": "PreToolUse", "toolId": "read",
                "toolTags": ["fs", "inspect"]})
            self.assertTrue((await client.list_kiro_hooks(trigger="preToolUse"))["hooks"])
            self.assertEqual(self.listings()[-1]["trigger"], "PreToolUse")
            await client.query("hello")
            self.assertEqual([message async for message in client.receive_response()][-1].result, "hello")

    async def test_invalid_filters_do_not_broaden_the_remote_query(self):
        async with KiroSDKClient(self.options()) as client:
            for kwargs in (
                {"trigger": "unrecognized"}, {"trigger": ""}, {"trigger": 1},
                {"tool_id": ""}, {"tool_id": "bad\nname"}, {"tool_id": "x" * 257},
                {"tool_tags": ["fs"]}, {"tool_id": "read", "tool_tags": ["fs", "fs"]},
                {"tool_id": "read", "tool_tags": [None]}, {"tool_id": "read", "tool_tags": ["x"] * 33},
                {"include_disabled": 1},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await client.list_kiro_hooks(**kwargs)
            self.assertEqual(self.listings(), [])

    async def test_invalid_response_is_not_treated_as_an_empty_list(self):
        env = {"KIRO_HOME": str(self.root / "kiro"), "FAKE_INVALID_HOOK_LIST": "1"}
        async with KiroSDKClient(self.options(env=env)) as client:
            with self.assertRaisesRegex(ProtocolError, "invalid hook listing"):
                await client.list_kiro_hooks()

    async def test_requires_active_v3_session_and_enabled_hooks(self):
        client = KiroSDKClient(self.options())
        with self.assertRaises(CLIConnectionError):
            await client.list_kiro_hooks()
        async with KiroSDKClient(self.options(agent_engine="v2")) as client:
            with self.assertRaisesRegex(ProtocolError, "Kiro V3"):
                await client.list_kiro_hooks()
        options = self.options(enable_kiro_hooks=False)
        async with KiroSDKClient(options) as client:
            with self.assertRaisesRegex(ProtocolError, "enabled Kiro"):
                await client.list_kiro_hooks()
            options.enable_kiro_hooks = True
            with self.assertRaisesRegex(ProtocolError, "Reconnect"):
                await client.list_kiro_hooks()

    async def test_remote_transport_lists_only_current_session_while_turn_is_active(self):
        class HookTransport(FakeTransport):
            async def write(self, data):
                message = json.loads(data)
                if message["method"] == "session/prompt":
                    self.requests.append(message)
                    self.prompt_id = message["id"]
                elif message["method"] == "_kiro/hooks/list":
                    self.requests.append(message)
                    await self.queue.put({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "hooks": [{"id": "remote-hook", "_meta": {"trigger": "PreToolUse"}}]}})
                elif message["method"] == "session/cancel":
                    self.requests.append(message)
                    await self.queue.put({"jsonrpc": "2.0", "id": self.prompt_id,
                                          "result": {"stopReason": "cancelled"}})
                else:
                    await super().write(data)

        transport = HookTransport()
        async with KiroSDKClient(KiroAgentOptions(agent_engine="v3"), transport=transport) as client:
            await client.query("pending")
            result = await asyncio.wait_for(client.list_kiro_hooks(trigger="preToolUse"), 2)
            self.assertEqual(result["hooks"][0]["id"], "remote-hook")
            request = next(item for item in transport.requests if item["method"] == "_kiro/hooks/list")
            self.assertEqual(request["params"], {
                "sessionId": "remote", "includeDisabled": False, "trigger": "PreToolUse"})
            self.assertFalse(client._turn.done())
            await client.interrupt()
            _ = [message async for message in client.receive_response()]
