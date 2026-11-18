import json
import unittest

from kiro_agent_sdk import AgentMessage, TextBlock, ToolCall
from kiro_agent_sdk.background_events import encode_background_event, encode_structured_result


class BackgroundEventEncodingTests(unittest.TestCase):
    def test_snapshot_copies_supported_message_data(self):
        arguments = {"shared": ["one"]}
        event = AgentMessage([TextBlock("hello"), {"type": "resource_link", "data": arguments}])
        encoded = encode_background_event(event, max_bytes=4096)
        arguments["shared"][0] = "changed"
        snapshot = json.loads(encoded)
        self.assertEqual(snapshot["type"], "AgentMessage")
        self.assertEqual(snapshot["message"]["content"][1]["data"], {"shared": ["one"]})
        tool = json.loads(encode_background_event(ToolCall("id", "tool", raw_input={"a": 1}), max_bytes=4096))
        self.assertEqual((tool["type"], tool["message"]["raw_input"]), ("ToolCall", {"a": 1}))

    def test_cycles_invalid_values_and_oversized_content_are_rejected(self):
        cycle = {}
        cycle["self"] = cycle
        for data in (cycle, {"bad": float("nan")}, {"bad": object()}, {"bad": 2**300},
                     {"bad": "x" * 4096}, {"bad": "\ud800"}):
            with self.subTest(data_type=type(data.get("bad", data)).__name__), self.assertRaises(ValueError):
                encode_background_event(ToolCall("id", "tool", raw_input=data), max_bytes=1024)

    def test_non_sdk_message_is_rejected(self):
        with self.assertRaises(ValueError):
            encode_background_event({"type": "unexpected"}, max_bytes=1024)

    def test_structured_result_accepts_bounded_json_and_rejects_non_json(self):
        self.assertEqual(json.loads(encode_structured_result({"ok": [1, True, None]}, max_bytes=128)),
                         {"ok": [1, True, None]})
        self.assertEqual(json.loads(encode_structured_result("é", max_bytes=4)), "é")
        with self.assertRaises(ValueError):
            encode_structured_result("é", max_bytes=3)
        cycle = []
        cycle.append(cycle)
        for value in (cycle, (1, 2), {"bad": object()}, {"bad": float("nan")}, "x" * 129):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                encode_structured_result(value, max_bytes=128)
