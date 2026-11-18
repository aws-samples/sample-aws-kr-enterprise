import copy
import os
import unittest
from unittest.mock import patch

from kiro_agent_sdk import (InMemorySessionStore, get_session_messages,
                            get_session_messages_from_store, project_key_for_directory)
from history_helpers import blocks, role


def chunk(text, message_id="one", role="agent", **context):
    return {"sessionUpdate": role + "_message_chunk", "messageId": message_id,
            "content": {"type": "text", "text": text}, **context}


class SessionCoalescingTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_consecutive_matching_ids_and_context_are_combined(self):
        events = [
            chunk("a"), chunk("b"),
            {"sessionUpdate": "tool_call", "toolCallId": "tool"},
            chunk("c"), chunk("d", _meta={"agent": "child"}),
            chunk("e", _meta={"agent": "parent"}),
            chunk("f", message_id=None), chunk("g", message_id=None),
            chunk("h", role="user"), chunk("i", role="user"),
            chunk("j", message_id="two"),
        ]
        original = copy.deepcopy(events)
        with patch("kiro_agent_sdk.sessions.get_session_history", return_value=events):
            default = await get_session_messages("session")
            grouped = await get_session_messages("session", coalesce_chunks=True)
            page = await get_session_messages("session", limit=2, offset=1, coalesce_chunks=True)
        self.assertEqual(len(default), 10)
        self.assertEqual([item.content[0].text for item in grouped],
                         ["ab", "c", "d", "e", "f", "g", "hi", "j"])
        self.assertEqual([role(item) for item in grouped],
                         ["agent", "agent", "agent", "agent",
                          "agent", "agent", "user", "agent"])
        self.assertEqual([item for item in page], [item for item in grouped[1:3]])
        grouped[0].content[0].text = "modified"
        self.assertEqual(events, original)

    async def test_multimodal_blocks_and_text_metadata_are_preserved(self):
        events = [
            chunk("a"),
            {"sessionUpdate": "agent_message_chunk", "messageId": "one",
             "content": {"type": "image", "data": "image", "mimeType": "image/png"}},
            chunk("b"),
            {"sessionUpdate": "agent_message_chunk", "messageId": "one",
             "content": {"type": "text", "text": "c", "annotations": {"audience": ["user"]}}},
            chunk("d"), chunk("e"),
        ]
        with patch("kiro_agent_sdk.sessions.get_session_history", return_value=events):
            messages = await get_session_messages("session", coalesce_chunks=True)
        self.assertEqual(blocks(messages[0]), [
            {"type": "text", "text": "a"},
            {"type": "image", "data": "image", "mimeType": "image/png"},
            {"type": "text", "text": "b"},
            {"type": "text", "text": "c", "annotations": {"audience": ["user"]}},
            {"type": "text", "text": "de"},
        ])

    async def test_mirror_boundaries_pagination_and_default(self):
        store = InMemorySessionStore()
        key = {"project_key": project_key_for_directory(os.getcwd()), "session_id": "session"}
        await store.append(key, [
            {"type": "prompt", "uuid": "user-1", "data": [{"type": "text", "text": "prompt"}]},
            {"type": "update", "data": chunk("a")},
            {"type": "update", "data": chunk("b")},
            {"type": "update", "data": chunk("do not duplicate", role="user")},
            {"type": "update", "data": chunk("c")},
            {"type": "update", "data": {"sessionUpdate": "tool_call"}},
            {"type": "update", "data": chunk("d")},
            {"type": "session_info", "data": {"title": "barrier"}},
            {"type": "update", "data": chunk("e")},
        ])
        default = await get_session_messages_from_store(store, "session")
        grouped = await get_session_messages_from_store(store, "session", coalesce_chunks=True)
        page = await get_session_messages_from_store(store, "session", limit=2, offset=1, coalesce_chunks=True)
        self.assertEqual(len(default), 6)
        self.assertEqual([item.content[0].text for item in grouped],
                         ["prompt", "ab", "c", "d", "e"])
        self.assertEqual([item for item in page], [item for item in grouped[1:3]])

    async def test_excluding_observed_subagents_preserves_parent_boundaries(self):
        events = [
            chunk("before"),
            chunk("child", _meta={"kiro": {"agentSubtaskId": "child-1"}}),
            chunk("after"), chunk(" parent"),
            # Missing or malformed IDs do not prove an event is from a child.
            chunk("unknown", message_id="unknown", _meta={"kiro": {"agentSubtaskId": 7}}),
            chunk("missing", message_id="missing", _meta={"kiro": {}}),
        ]
        with patch("kiro_agent_sdk.sessions.get_session_history", return_value=events):
            default = await get_session_messages("session", coalesce_chunks=True)
            filtered = await get_session_messages(
                "session", coalesce_chunks=True, exclude_observed_subagents=True)
            page = await get_session_messages("session", limit=2, offset=1,
                                              coalesce_chunks=True, exclude_observed_subagents=True)
        self.assertEqual([item.content[0].text for item in default],
                         ["before", "child", "after parent", "unknown", "missing"])
        self.assertEqual([item.content[0].text for item in filtered],
                         ["before", "after parent", "unknown", "missing"])
        self.assertEqual([item for item in page], [item for item in filtered[1:3]])

        store = InMemorySessionStore()
        key = {"project_key": project_key_for_directory(os.getcwd()), "session_id": "session"}
        await store.append(key, [{"type": "update", "data": event} for event in events])
        mirrored = await get_session_messages_from_store(
            store, "session", coalesce_chunks=True, exclude_observed_subagents=True)
        self.assertEqual([item for item in mirrored], [item for item in filtered])

    async def test_invalid_option_fails_before_loading(self):
        store = InMemorySessionStore()
        with patch("kiro_agent_sdk.sessions.get_session_history") as history:
            with self.assertRaisesRegex(ValueError, "coalesce_chunks"):
                await get_session_messages("session", coalesce_chunks=1)
            with self.assertRaisesRegex(ValueError, "exclude_observed_subagents"):
                await get_session_messages("session", exclude_observed_subagents="yes")
            history.assert_not_called()
        with patch.object(store, "load") as load:
            with self.assertRaisesRegex(ValueError, "coalesce_chunks"):
                await get_session_messages_from_store(store, "session", coalesce_chunks="yes")
            with self.assertRaisesRegex(ValueError, "exclude_observed_subagents"):
                await get_session_messages_from_store(store, "session", exclude_observed_subagents=1)
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
