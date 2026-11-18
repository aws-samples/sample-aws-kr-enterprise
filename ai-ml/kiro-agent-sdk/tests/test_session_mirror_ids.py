import asyncio
import tempfile
import unittest

from kiro_agent_sdk import (HookMatcher, InMemorySessionStore, KiroAgentOptions, KiroSDKClient,
                            ResultError, ResultMessage, KiroEvent, get_session_messages_from_store,
                            project_key_for_directory)
from test_user_message_replay import ReplayTransport
from history_helpers import blocks, role


class SessionMirrorIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_turns_link_unique_observed_ids_without_replay_option(self):
        with tempfile.TemporaryDirectory() as root:
            store = InMemorySessionStore()
            transport = ReplayTransport([{"userMessageId": "first-user"}])
            options = KiroAgentOptions(cwd=root, agent_engine="v3", session_store=store)
            async with KiroSDKClient(options, transport=transport) as client:
                results = []
                for text, reported in (("first", "first-user"), ("second", "second-user")):
                    transport.updates = [{"userMessageId": reported}]
                    await client.query(text)
                    messages = [item async for item in client.receive_response()]
                    results.append(next(item for item in messages if isinstance(item, ResultMessage)))
                    for item in messages:
                        if isinstance(item, KiroEvent):
                            self.assertNotIn("_sdk_replay_turn", item.data)
                stored = await get_session_messages_from_store(store, "remote", directory=root)
                users = [item for item in stored if role(item) == "user"]
                self.assertEqual([item.message_id for item in users], ["first-user", "second-user"])
                self.assertEqual([item.message_id for item in users], [item.user_message_id for item in results])
                entries = await store.load({"project_key": project_key_for_directory(root), "session_id": "remote"})
                keys = [item["kiro_sdk_prompt_key"] for item in entries if item["type"] == "prompt"]
                self.assertEqual(len(set(keys)), 2)
                self.assertTrue(all(len(key) == 32 for key in keys))
                self.assertEqual((await get_session_messages_from_store(
                    store, "remote", directory=root, limit=1, offset=2))[0].message_id, "second-user")

    async def test_prompts_keep_their_block_form(self):
        async def add_context(event, tool_id, context):
            if event["prompt"] == "third":
                return {'context': "extra context"}
            return {}
        with tempfile.TemporaryDirectory() as root:
            store = InMemorySessionStore()
            options = KiroAgentOptions(cwd=root, agent_engine="v3", session_store=store,
                hooks={"promptSubmit": [HookMatcher(hooks=[add_context])]})
            transport = ReplayTransport([{"userMessageId": "first-user"}])
            async with KiroSDKClient(options, transport=transport) as client:
                for prompt in ("first", [{"type": "text", "text": "second"}], "third"):
                    await client.query(prompt)
                    _ = [item async for item in client.receive_response()]
                stored = await get_session_messages_from_store(store, "remote", directory=root)
            users = [item for item in stored if role(item) == "user"]
            self.assertEqual([blocks(item) for item in users], [
                [{"type": "text", "text": "first"}], [{"type": "text", "text": "second"}],
                [{"type": "text", "text": "third"}, {"type": "text", "text": "extra context"}]])
            self.assertEqual([item.message_id for item in users], ["first-user"] * 3)

    async def test_missing_conflicting_invalid_and_subagent_ids_are_not_guessed(self):
        variants = [
            [], [{"userMessageId": ""}], [{"userMessageId": "bad\nid"}],
            [{"userMessageId": "x" * 513}], [{"userMessageId": "one"}, {"userMessageId": "two"}],
            [{"userMessageId": "one"}, {"userMessageId": None}],
            [{"userMessageId": "one"}, {"userMessageId": "child", "agentSubtaskId": 7}],
            [{"userMessageId": "one"}, {"userMessageId": "child", "agentSubtaskId": None}],
            [{"userMessageId": "one"}, {"userMessageId": "child", "agentSubtaskId": ""}],
            [{"userMessageId": "child", "agentSubtaskId": "child-1"}],
        ]
        for updates in variants:
            with self.subTest(updates=updates), tempfile.TemporaryDirectory() as root:
                store = InMemorySessionStore()
                transport = ReplayTransport(updates)
                options = KiroAgentOptions(cwd=root, agent_engine="v3", session_store=store)
                async with KiroSDKClient(options, transport=transport) as client:
                    await client.query("hello")
                    _ = [item async for item in client.receive_response()]
                    stored = await get_session_messages_from_store(store, "remote", directory=root)
                self.assertIsNone(next(item for item in stored if role(item) == "user").message_id)

    async def test_identified_child_does_not_override_unique_parent(self):
        updates = [{"userMessageId": "child", "agentSubtaskId": "child-1"},
                   {"userMessageId": "parent"},
                   {"userMessageId": "child-2", "agentSubtaskId": "child-1"}]
        with tempfile.TemporaryDirectory() as root:
            store = InMemorySessionStore()
            async with KiroSDKClient(KiroAgentOptions(cwd=root, agent_engine="v3", session_store=store),
                                     transport=ReplayTransport(updates)) as client:
                await client.query("hello")
                _ = [item async for item in client.receive_response()]
                stored = await get_session_messages_from_store(store, "remote", directory=root)
            self.assertEqual(next(item for item in stored if role(item) == "user").message_id, "parent")

    async def test_idle_notification_cannot_supply_a_later_prompt_id(self):
        with tempfile.TemporaryDirectory() as root:
            store, transport = InMemorySessionStore(), ReplayTransport()
            async with KiroSDKClient(KiroAgentOptions(cwd=root, agent_engine="v3", session_store=store),
                                     transport=transport) as client:
                await transport.queue.put({"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": "remote", "update": {"sessionUpdate": "session_info_update", "_meta": {
                        "kiro": {"kind": "user_message_id_assigned", "userMessageId": "stale"}}}}})
                for _ in range(100):
                    if not client._events.empty():
                        break
                    await asyncio.sleep(.001)
                self.assertFalse(client._events.empty())
                await client.query("hello")
                _ = [item async for item in client.receive_response()]
                stored = await get_session_messages_from_store(store, "remote", directory=root)
            self.assertIsNone(next(item for item in stored if role(item) == "user").message_id)

    async def test_missing_result_after_structured_output_failure_keeps_id_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            store = InMemorySessionStore()
            options = KiroAgentOptions(cwd=root, agent_engine="v3", session_store=store,
                                       output_format={"type": "json_schema", "schema": {"type": "object"}})
            async with KiroSDKClient(options, transport=ReplayTransport([{"userMessageId": "user"}])) as client:
                await client.query("not json")
                with self.assertRaises(ResultError):
                    _ = [item async for item in client.receive_response()]
                stored = await get_session_messages_from_store(store, "remote", directory=root)
                self.assertIsNone(next(item for item in stored if role(item) == "user").message_id)

    async def test_ambiguous_or_out_of_order_mirror_records_are_not_linked(self):
        with tempfile.TemporaryDirectory() as root:
            store = InMemorySessionStore()
            key = {"project_key": project_key_for_directory(root), "session_id": "session"}
            def prompt(token, text, **extra):
                return {"type": "prompt", "kiro_sdk_prompt_key": token,
                        "data": [{"type": "text", "text": text}], **extra}
            def result(token, value):
                return {"type": "result", "kiro_sdk_prompt_key": token,
                        "data": {"kiro_sdk_unique_user_message_id": value}}
            a, b, c, d, e, f = [str(i) * 32 for i in range(1, 7)]
            await store.append(key, [
                prompt(a, "valid"), result(a, "observed"),
                prompt(b, "duplicate prompt 1"), prompt(b, "duplicate prompt 2"), result(b, "unsafe"),
                prompt(c, "duplicate result"), result(c, "one"), result(c, "two"),
                result(d, "too early"), prompt(d, "out of order"),
                prompt(e, "invalid id"), result(e, "bad\nid"),
                prompt(f, "explicit", uuid="legacy"), result(f, "new"),
                prompt("not-a-key", "invalid key"), result("not-a-key", "unsafe"),
            ])
            messages = await get_session_messages_from_store(store, "session", directory=root)
            self.assertEqual([item.message_id for item in messages],
                             ["observed", None, None, None, None, None, "legacy", None])

    async def test_one_shot_iterable_store_is_read_once_without_losing_entries(self):
        token = "a" * 32
        entries = [
            {"type": "prompt", "kiro_sdk_prompt_key": token, "data": [{"type": "text", "text": "hello"}]},
            {"type": "result", "kiro_sdk_prompt_key": token,
             "data": {"kiro_sdk_unique_user_message_id": "observed"}},
        ]
        class IterableStore:
            async def load(self, key):
                return iter(entries)
        messages = await get_session_messages_from_store(IterableStore(), "session")
        self.assertEqual((messages[0].message_id, blocks(messages[0])), ("observed", [{"type": "text", "text": "hello"}]))


if __name__ == "__main__":
    unittest.main()
