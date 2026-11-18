import copy
import os
import unittest
from unittest.mock import patch

from kiro_agent_sdk import (InMemorySessionStore, ToolCall, get_session_messages, get_session_messages_from_store,
                            project_key_for_directory)
from kiro_agent_sdk.replay_tools import observed_tool_messages
from history_helpers import role


def update(kind="tool_call", tool_id="call-1", **fields):
    return {"sessionUpdate": kind, "toolCallId": tool_id, **fields}


def kiro(**fields):
    return {"kiro": fields}


class ObservedToolProjectionTests(unittest.TestCase):
    def test_native_replay_merges_the_call_and_its_updates(self):
        inputs = {"tool_id": "tool::probe::echo", "arguments": {"value": "synthetic"}}
        content = [{"type": "content", "content": {"type": "text", "text": "RESULT"}}]
        records = [
            update(title="@probe/echo", status="pending", rawInput=inputs,
                   _meta=kiro(turnId="turn", executionId="execution")),
            update("tool_call_update", status="completed", rawOutput="RESULT", content=content,
                   _meta=kiro(turnId="turn", executionId="execution")),
        ]
        original = copy.deepcopy(records)
        messages = observed_tool_messages(records, "session")
        self.assertEqual(set(messages), {0})
        self.assertEqual(messages[0], ToolCall("call-1", "@probe/echo", status="completed",
                                               raw_input=inputs, raw_output="RESULT", content=content,
                                               session_id="session",
                                               kiro_metadata={"turnId": "turn", "executionId": "execution"}))
        messages[0].raw_input["arguments"]["value"] = "changed"
        self.assertEqual(records, original)

    def test_duplicate_ambiguous_or_unbounded_calls_are_not_projected(self):
        valid = update(title="probe", rawInput={"value": 1})
        cycle = {}
        cycle["cycle"] = cycle
        class ForeignLabel(str):
            def strip(self):
                raise AssertionError("custom label behavior was invoked")
        cases = [
            [valid, update(title="probe", rawInput={"value": 1})],
            [valid, update("tool_call_update", rawInput="invalid")],
            [update(title="probe", rawInput={1: "invalid"})],
            [update(title="probe", rawInput={"tuple": (1, 2)})],
            [update(title="probe", rawInput=cycle)],
            [update(title="bad\ud800", rawInput={})],
            [update(title=ForeignLabel("probe"), rawInput={})],
            [update(tool_id="bad\ud800", title="probe", rawInput={})],
            [valid, update("tool_call_update", _meta=kiro(agentSubtaskId="child"))],
            [valid, update("tool_call_update", _meta=kiro(agentSubtaskId=None))],
            [valid, update("tool_call_update", _meta={"kiro": None})],
            [update(title="probe", rawInput={"text": "x" * (1024 * 1024 + 1)})],
        ]
        for events in cases:
            with self.subTest(events=str(events)[:200]):
                self.assertEqual(observed_tool_messages(events, "session"), {})

    def test_updates_must_follow_the_call_in_the_same_context(self):
        call = update(title="probe", rawInput={"x": 1}, _meta=kiro(turnId="turn", executionId="exec"))
        done = update("tool_call_update", status="completed", rawOutput="ok",
                      _meta=kiro(turnId="turn", executionId="exec"))
        self.assertEqual(observed_tool_messages([call, done], "session")[0].status, "completed")
        cases = [
            [call, update("tool_call_update", status="completed", _meta=kiro(turnId="other", executionId="exec"))],
            [call, update("tool_call_update", status="completed", _meta=kiro(turnId="turn"))],
            [update(title="probe", rawInput={}, _meta=kiro(serverName="one")),
             update("tool_call_update", status="completed", _meta=kiro(serverName="other"))],
            [done, call],
        ]
        for events in cases:
            with self.subTest(events=events):
                self.assertEqual(observed_tool_messages(events, "session"), {})

    def test_failed_status_and_title_updates_are_reported(self):
        call = update(title="probe", rawInput={})
        result = update("tool_call_update", title="probe (failed)", status="failed", rawOutput="failure")
        projected = observed_tool_messages([call, result], "session")
        self.assertEqual((projected[0].title, projected[0].status, projected[0].raw_output),
                         ("probe (failed)", "failed", "failure"))

    def test_tracking_limits_drop_ambiguous_tools_without_affecting_earlier_ids(self):
        with patch("kiro_agent_sdk.replay_tools._MAX_TOOL_IDS", 1), \
             patch("kiro_agent_sdk.replay_tools._MAX_EVENTS_PER_TOOL", 1):
            first = update(title="probe", rawInput={})
            second = update(tool_id="other", title="other", rawInput={})
            projected = observed_tool_messages([first, second], "session")
            self.assertEqual(set(projected), {0})
            repeated = [first, update("tool_call_update"), update("tool_call_update")]
            self.assertEqual(observed_tool_messages(repeated, "session"), {})


class ObservedToolSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_option_preserves_order_and_paginates_after_projection(self):
        events = [
            {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "question"}},
            update(title="@probe/echo", rawInput={"value": "synthetic"}, _meta=kiro(turnId="turn", executionId="exec")),
            update("tool_call_update", status="completed", rawOutput="result",
                   _meta=kiro(turnId="turn", executionId="exec")),
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "answer"}},
        ]
        with patch("kiro_agent_sdk.sessions.get_session_history", return_value=events):
            default = await get_session_messages("session")
            messages = await get_session_messages("session", include_observed_tool_messages=True)
            page = await get_session_messages("session", include_observed_tool_messages=True, limit=2, offset=1)
        self.assertEqual([role(m) for m in default], ["user", "agent"])
        self.assertEqual([role(m) for m in messages], ["user", "tool", "agent"])
        self.assertEqual((messages[1].raw_input, messages[1].raw_output), ({"value": "synthetic"}, "result"))
        self.assertEqual(page, messages[1:3])

    async def test_mirror_option_reports_the_tool_without_exposing_unrelated_updates(self):
        store = InMemorySessionStore()
        key = {"project_key": project_key_for_directory(os.getcwd()), "session_id": "session"}
        prompt_key = "a" * 32
        inputs = {"arguments": {"value": "synthetic"}}
        content = [{"type": "content", "content": {"type": "text", "text": "result"}}]
        await store.append(key, [
            {"type": "prompt", "kiro_sdk_prompt_key": prompt_key,
             "data": [{"type": "text", "text": "question"}]},
            {"type": "update", "kiro_sdk_prompt_key": prompt_key,
             "data": update(title="@probe/echo", rawInput=inputs, _meta=kiro(serverName="probe"))},
            {"type": "notification", "data": {"method": "fixture"}},
            {"type": "update", "kiro_sdk_prompt_key": prompt_key,
             "data": update("tool_call_update", status="completed", rawInput=inputs, rawOutput="result",
                            content=content, _meta=kiro(serverName="probe"))},
            {"type": "update", "data": {"sessionUpdate": "agent_message_chunk",
                                         "content": {"type": "text", "text": "answer"}}},
            {"type": "result", "kiro_sdk_prompt_key": prompt_key, "data": {}},
        ])
        messages = await get_session_messages_from_store(store, "session", include_observed_tool_messages=True)
        self.assertEqual([role(m) for m in messages], ["user", "tool", "agent"])
        self.assertEqual((messages[1].raw_input, messages[1].content), (inputs, content))

    async def test_mirror_refuses_orphan_cross_turn_and_duplicate_keys(self):
        store = InMemorySessionStore()
        key = {"project_key": project_key_for_directory(os.getcwd()), "session_id": "session"}
        a, b, c, d = [value * 32 for value in ("a", "b", "c", "d")]
        call = update(title="probe", rawInput={})
        result = update("tool_call_update", status="completed", rawOutput="result")
        await store.append(key, [
            {"type": "prompt", "kiro_sdk_prompt_key": a, "data": []},
            {"type": "update", "kiro_sdk_prompt_key": a, "data": call},
            {"type": "result", "kiro_sdk_prompt_key": a, "data": {}},
            {"type": "prompt", "kiro_sdk_prompt_key": b, "data": []},
            {"type": "update", "kiro_sdk_prompt_key": b, "data": result},
            {"type": "result", "kiro_sdk_prompt_key": b, "data": {}},
            {"type": "prompt", "kiro_sdk_prompt_key": c, "data": []},
            {"type": "update", "kiro_sdk_prompt_key": c, "data": call},
            {"type": "update", "kiro_sdk_prompt_key": c, "data": result},
            {"type": "result", "kiro_sdk_prompt_key": c, "data": {}},
            {"type": "result", "kiro_sdk_prompt_key": c, "data": {}},
            {"type": "prompt", "kiro_sdk_prompt_key": d, "data": []},
            {"type": "update", "kiro_sdk_prompt_key": d, "data": call},
            {"type": "update", "kiro_sdk_prompt_key": d, "data": result},
            {"type": "result", "kiro_sdk_prompt_key": d, "data": {}},
            {"type": "update", "kiro_sdk_prompt_key": d, "data": result},
            {"type": "prompt", "data": []},
            {"type": "update", "data": call}, {"type": "update", "data": result},
            {"type": "result", "data": {}},
        ])
        messages = await get_session_messages_from_store(store, "session", include_observed_tool_messages=True)
        # Only the first turn's call is unambiguous; Kiro reported no result for it in that turn.
        self.assertEqual([m.status for m in messages if role(m) == "tool"], [None])

    async def test_mirror_refuses_unlinked_or_malformed_tool_updates_within_turn(self):
        prompt_key, other_key = "a" * 32, "b" * 32
        call = update(title="probe", rawInput={})
        result = update("tool_call_update", status="completed", rawOutput="result")
        variants = [
            {"type": "update", "data": update("tool_call_update", rawInput={"changed": True})},
            {"type": "update", "kiro_sdk_prompt_key": other_key,
             "data": update("tool_call", tool_id="other", title="other", rawInput={})},
            {"type": "update", "kiro_sdk_prompt_key": prompt_key, "data": "malformed"},
            {"type": "update", "kiro_sdk_prompt_key": prompt_key, "data": {}},
        ]
        for extra in variants:
            with self.subTest(extra=extra):
                store = InMemorySessionStore()
                key = {"project_key": project_key_for_directory(os.getcwd()), "session_id": "session"}
                await store.append(key, [
                    {"type": "prompt", "kiro_sdk_prompt_key": prompt_key, "data": []},
                    {"type": "update", "kiro_sdk_prompt_key": prompt_key, "data": call},
                    extra,
                    {"type": "update", "kiro_sdk_prompt_key": prompt_key, "data": result},
                    {"type": "result", "kiro_sdk_prompt_key": prompt_key, "data": {}},
                ])
                messages = await get_session_messages_from_store(
                    store, "session", include_observed_tool_messages=True)
                self.assertFalse(any(role(m) == "tool" for m in messages))

    async def test_tool_option_rejects_invalid_types_and_archive_combination(self):
        store = InMemorySessionStore()
        with patch("kiro_agent_sdk.sessions.get_session_history") as history, patch.object(store, "load") as load:
            with self.assertRaisesRegex(ValueError, "include_observed_tool_messages"):
                await get_session_messages("session", include_observed_tool_messages=1)
            with self.assertRaisesRegex(ValueError, "include_observed_tool_messages"):
                await get_session_messages_from_store(store, "session", include_observed_tool_messages="yes")
            with self.assertRaisesRegex(ValueError, "Replay options"):
                await get_session_messages("session", agent_engine="v3", kiro_history_source="archive-tools",
                                           include_observed_tool_messages=True)
            history.assert_not_called()
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
