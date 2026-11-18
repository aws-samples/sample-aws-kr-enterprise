import io
import json
import unittest
import zipfile
from unittest.mock import AsyncMock, patch

from kiro_agent_sdk import (
    AgentMessage,
    InMemorySessionStore,
    SessionArchiveError,
    get_session_archive_history,
    get_session_archive_history_from_store,
    get_session_archive_messages,
    get_session_archive_messages_from_store,
    get_session_messages,
    project_key_for_directory,
    TextBlock,
    ToolCall,
    UserMessage,
)
from kiro_agent_sdk.archive import _archive_payload


def archive_data():
    records = [
        {"id": "user-id", "payload": {"type": "user", "content": "Hello", "images": [{"data": "abc"}]}},
        {"id": "thought-id", "payload": {"type": "assistant", "operationType": "Reasoning", "content": "private"}},
        {"id": "tool-id", "payload": {"type": "tool_result", "content": "tool output"}},
        {"id": "answer-id", "payload": {"type": "assistant", "operationType": "Say", "content": "Hi"}},
    ]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("session.json", "{}")
        archive.writestr("messages.jsonl", "\n".join(json.dumps(record) for record in records))
    return output.getvalue(), records


def make_archive(records):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("session.json", "{}")
        archive.writestr("messages.jsonl", "\n".join(json.dumps(record) for record in records))
    return output.getvalue()


def shape(message):
    return (type(message).__name__, message.tool_call_id if isinstance(message, ToolCall) else message.message_id)


def mirrored_prompt(message_id="user-id", text="Hello", *, key="a" * 32):
    return [
        {"type": "prompt", "kiro_sdk_prompt_key": key, "data": [{"type": "text", "text": text}]},
        {"type": "result", "kiro_sdk_prompt_key": key,
         "data": {"kiro_sdk_unique_user_message_id": message_id}},
    ]


def user_record(message_id="user-id", text="Hello", **fields):
    return {"id": message_id, "payload": {
        "type": "user", "content": text, "images": [], "documents": [], "_meta": {}, **fields}}


def tool_call_record(tool_id="call-1", *, record_id="call-record", **fields):
    return {"id": record_id, "payload": {"type": "tool_call", "toolCallId": tool_id,
            "toolName": "tool_call", "title": "@probe_server/probe",
            "args": {"value": "synthetic"}, "_meta": {"kiro": {}}, **fields}}


def tool_result_record(tool_id="call-1", *, record_id="result-record", **fields):
    return {"id": record_id, "payload": {"type": "tool_result", "toolCallId": tool_id,
            "success": True, "content": "SYNTHETIC_RESULT", "_meta": {"kiro": {}}, **fields}}


class SessionArchiveHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_message_api_can_choose_v3_archive_text_preview(self):
        archive, _ = archive_data()
        env = {"KIRO_HOME": "/isolated/kiro"}
        with patch("kiro_agent_sdk.session_archive_history.export_session_archive",
                   new=AsyncMock(return_value=archive)) as export:
            messages = await get_session_messages("s", cwd="/workspace", env=env, agent_engine="v3",
                                                  kiro_history_source="archive-text", limit=1, offset=0)
        self.assertEqual([shape(m) for m in messages], [("UserMessage", "user-id")])
        self.assertEqual(messages[0].content, [TextBlock("Hello")])
        self.assertEqual(export.await_args.kwargs["cwd"], "/workspace")
        self.assertEqual(export.await_args.kwargs["env"], env)

    async def test_archive_source_rejects_incompatible_options_before_export(self):
        with patch("kiro_agent_sdk.session_archive_history.export_session_archive") as export, \
             patch("kiro_agent_sdk.sessions.get_session_history") as replay:
            for values in ({"kiro_history_source": "other"}, {"kiro_history_source": "archive-text"},
                           {"kiro_history_source": "archive-text", "agent_engine": "v2"},
                           {"kiro_history_source": "archive-text", "agent_engine": "v3", "coalesce_chunks": True},
                           {"kiro_history_source": "archive-text", "agent_engine": "v3",
                            "exclude_observed_subagents": True}):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    await get_session_messages("s", **values)
            export.assert_not_called()
            replay.assert_not_called()

    async def test_archive_tool_preview_merges_reported_call_and_result(self):
        records = [user_record(), tool_call_record(), tool_result_record(),
                   {"id": "answer", "payload": {"type": "assistant", "content": "Done"}}]
        archive = make_archive(records)
        with patch("kiro_agent_sdk.session_archive_history.export_session_archive",
                   new=AsyncMock(return_value=archive)):
            default = await get_session_archive_messages("s")
            messages = await get_session_archive_messages("s", include_tool_messages=True)
            through_session_api = await get_session_messages(
                "s", agent_engine="v3", kiro_history_source="archive-tools", limit=2, offset=1)
        self.assertEqual([type(m) for m in default], [UserMessage, AgentMessage])
        self.assertEqual([shape(m) for m in messages], [
            ("UserMessage", "user-id"), ("ToolCall", "call-1"), ("AgentMessage", "answer")])
        self.assertEqual(messages[1], ToolCall("call-1", "@probe_server/probe", status="completed",
                                               raw_input={"value": "synthetic"}, raw_output="SYNTHETIC_RESULT",
                                               session_id="s", kiro_metadata={"toolName": "tool_call"}))
        self.assertEqual(through_session_api, messages[1:3])

    async def test_archive_tool_preview_skips_ambiguous_and_unreported_fields(self):
        variants = [
            ([tool_call_record(), tool_call_record(record_id="duplicate"), tool_result_record()], []),
            ([tool_call_record(args=None), tool_result_record()], []),
            ([tool_call_record(toolName=""), tool_result_record()], []),
            ([tool_call_record(toolName="bad\ud800"), tool_result_record()], []),
            ([tool_call_record(tool_id="bad\nid"), tool_result_record(tool_id="bad\nid")], []),
            ([tool_call_record(tool_id="bad\ud800"), tool_result_record(tool_id="bad\ud800")], []),
            ([tool_call_record(_meta=None), tool_result_record()], []),
            ([tool_call_record(_meta={"kiro": {"agentSubtaskId": "child"}}), tool_result_record()], []),
            ([tool_call_record(), tool_call_record(record_id="child",
              _meta={"kiro": {"agentSubtaskId": "child"}}), tool_result_record()], []),
            ([tool_call_record(), tool_result_record(), tool_result_record(record_id="duplicate")], [None]),
            ([tool_result_record(), tool_call_record()], [None]),
            ([tool_call_record(), tool_result_record(success=None)], [None]),
            ([tool_call_record(turnId="one", executionId="exec"),
              tool_result_record(turnId="two", executionId="exec")], [None]),
            ([tool_call_record(turnId="one"), tool_result_record()], [None]),
            ([tool_call_record(turnId=7), tool_result_record(turnId=7)], [None]),
            ([tool_call_record(turnId="bad\nid"), tool_result_record(turnId="bad\nid")], [None]),
            ([tool_call_record(), tool_result_record(content=[{"type": "image", "data": "x"}])], ["completed"]),
            ([tool_call_record(), tool_result_record(_meta={"kiro": {"agentSubtaskId": None}})], [None]),
        ]
        for records, expected in variants:
            with self.subTest(records=records):
                with patch("kiro_agent_sdk.session_archive_history.export_session_archive",
                           new=AsyncMock(return_value=make_archive(records))):
                    messages = await get_session_archive_messages("s", include_tool_messages=True)
                self.assertEqual([m.status for m in messages if isinstance(m, ToolCall)], expected)

    async def test_stored_archive_reports_tool_failure(self):
        records = [user_record(), tool_call_record(), tool_result_record(success=False, content="FAILED")]
        store = InMemorySessionStore()
        key = {"project_key": project_key_for_directory("."), "session_id": "s"}
        await store.append(key, mirrored_prompt() + [
            {"type": "session_archive", "data": _archive_payload("s", make_archive(records))}])
        messages = await get_session_archive_messages_from_store(store, "s", directory=".", include_tool_messages=True)
        self.assertEqual(messages[0].content, [TextBlock("Hello")])
        self.assertEqual((messages[-1].status, messages[-1].raw_output), ("failed", "FAILED"))
        with self.assertRaisesRegex(ValueError, "include_tool_messages"):
            await get_session_archive_messages_from_store(store, "s", include_tool_messages=1)

    async def test_archive_tool_result_is_copied(self):
        blocks = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
        records = [tool_call_record(turnId="turn", executionId="exec"),
                   tool_result_record(content=blocks, turnId="turn", executionId="exec")]
        with patch("kiro_agent_sdk.session_archive_history.export_session_archive",
                   new=AsyncMock(return_value=make_archive(records))):
            messages = await get_session_archive_messages("s", include_tool_messages=True)
        self.assertEqual(messages[-1].raw_output, blocks)
        messages[-1].raw_output[0]["text"] = "changed"
        self.assertEqual(records[-1]["payload"]["content"][0]["text"], "first")

    async def test_local_raw_records_and_text_preview(self):
        archive, records = archive_data()
        env = {"KIRO_HOME": "/isolated/kiro"}
        with patch("kiro_agent_sdk.session_archive_history.export_session_archive",
                   new=AsyncMock(return_value=archive)) as export:
            raw = await get_session_archive_history("s", cwd="/workspace", env=env, limit=2, offset=1)
            messages = await get_session_archive_messages("s", cwd="/workspace", env=env)
        self.assertEqual(raw, records[1:3])
        self.assertEqual([shape(m) for m in messages], [("UserMessage", "user-id"), ("AgentMessage", "answer-id")])
        self.assertEqual(messages[1].content, [TextBlock("Hi")])
        self.assertEqual(export.await_args.kwargs["env"], env)

    async def test_stored_snapshot_and_staleness(self):
        archive, records = archive_data()
        store = InMemorySessionStore()
        key = {"project_key": project_key_for_directory("."), "session_id": "s"}
        await store.append(key, [{"type": "session_archive", "data": _archive_payload("s", archive)}])
        self.assertEqual(await get_session_archive_history_from_store(store, "s", directory="."), records)
        preview = await get_session_archive_messages_from_store(store, "s", directory=".", limit=1, offset=1)
        self.assertEqual(preview[0].message_id, "answer-id")
        await store.append(key, [{"type": "prompt", "data": []}])
        with self.assertRaises(SessionArchiveError):
            await get_session_archive_history_from_store(store, "s", directory=".")
        self.assertEqual(await get_session_archive_history_from_store(store, "s", directory=".", allow_stale=True),
                         records)

    async def test_missing_archive_and_record_limit(self):
        store = InMemorySessionStore()
        key = {"project_key": project_key_for_directory("."), "session_id": "s"}
        self.assertEqual(await get_session_archive_history_from_store(store, "s", directory="."), [])
        await store.append(key, [{"type": "prompt", "data": []}])
        with self.assertRaises(SessionArchiveError):
            await get_session_archive_history_from_store(store, "s", directory=".")
        archive, _ = archive_data()
        with patch("kiro_agent_sdk.session_archive_history.export_session_archive", new=AsyncMock(return_value=archive)):
            with self.assertRaisesRegex(SessionArchiveError, "message count"):
                await get_session_archive_history("s", max_messages=2)

    async def test_stale_archive_requires_explicit_permission(self):
        store = InMemorySessionStore()
        key = {"project_key": project_key_for_directory("."), "session_id": "s"}
        await store.append(key, [
            {"type": "session_archive", "data": _archive_payload("s", make_archive([user_record()]))},
            *mirrored_prompt(),
        ])
        with self.assertRaises(SessionArchiveError):
            await get_session_archive_messages_from_store(store, "s", directory=".")
        messages = await get_session_archive_messages_from_store(store, "s", directory=".", allow_stale=True)
        self.assertEqual(messages[0].content, [TextBlock("Hello")])

    async def test_snapshot_is_loaded_once(self):
        entries = mirrored_prompt() + [
            {"type": "session_archive", "data": _archive_payload("s", make_archive([user_record()]))}]
        class SingleReadStore:
            calls = 0
            async def load(self, key):
                self.calls += 1
                if self.calls != 1:
                    raise AssertionError("store snapshot was read twice")
                return entries
        store = SingleReadStore()
        messages = await get_session_archive_messages_from_store(store, "s")
        self.assertEqual(messages[0].content, [TextBlock("Hello")])
        self.assertEqual(store.calls, 1)


if __name__ == "__main__":
    unittest.main()
