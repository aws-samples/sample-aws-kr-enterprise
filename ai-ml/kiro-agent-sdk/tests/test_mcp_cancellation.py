import json
import unittest
from unittest.mock import patch

from kiro_agent_sdk.mcp_cancellation import ObservedToolCalls, cancellation_notice, parse_message


class ObservedToolCallsTests(unittest.TestCase):
    def test_tracks_only_unambiguous_inflight_tool_requests(self):
        tracker = ObservedToolCalls()
        tracker.request({"method": "tools/call", "id": 1})
        tracker.request({"method": "tools/call", "id": "1"})
        tracker.request({"method": "tools/list", "id": 2})
        tracker.request({"method": "tools/call"})
        self.assertEqual(tracker.ids(), [1, "1"])
        tracker.response({"method": "ping", "id": 1})
        self.assertEqual(tracker.ids(), [1, "1"])
        tracker.response({"id": 1, "result": {}})
        self.assertEqual(tracker.ids(), ["1"])
        tracker.response({"id": "1", "error": {}})
        self.assertEqual(tracker.ids(), [])

    def test_duplicate_and_excess_ids_are_not_cancelled(self):
        tracker = ObservedToolCalls()
        with patch("kiro_agent_sdk.mcp_cancellation._MAX_TRACKED", 2):
            tracker.request({"method": "tools/call", "id": 1})
            tracker.request({"method": "tools/call", "id": 1})
            tracker.request({"method": "tools/call", "id": 2})
            tracker.request({"method": "tools/call", "id": 3})
            self.assertEqual(tracker.ids(), [2])
            tracker.response({"id": 1, "result": {}})
            tracker.request({"method": "tools/call", "id": 1})
            self.assertEqual(tracker.ids(), [2])
            tracker.response({"id": 2, "result": {}})
            tracker.request({"method": "tools/call", "id": 3})
            tracker.request({"method": "tools/call", "id": 4})
            self.assertEqual(tracker.ids(), [])

    def test_malformed_and_ambiguous_messages_are_not_tracked(self):
        self.assertIsNone(parse_message(b'{"id":1,"method":"tools/call","method":"ping"}'))
        self.assertIsNone(parse_message(b'[{"id":1,"method":"tools/call"}]'))
        self.assertIsNone(parse_message(b'{"id":NaN}'))
        self.assertEqual(parse_message(b'{"id":"a","method":"tools/call"}')["id"], "a")
        notice = cancellation_notice("a")
        self.assertEqual(notice["method"], "notifications/cancelled")
        self.assertEqual(notice["params"]["requestId"], "a")
        json.dumps(notice)

    def test_suppressed_id_cannot_be_reused_or_forward_a_late_response(self):
        tracker = ObservedToolCalls()
        self.assertTrue(tracker.request({"id": 7, "method": "tools/call"}))
        self.assertTrue(tracker.suppress(7))
        self.assertFalse(tracker.response({"id": 7, "result": {}}))
        self.assertFalse(tracker.request({"id": 7, "method": "tools/call"}))
        self.assertFalse(tracker.request({"id": 7, "method": "tools/list"}))
        self.assertTrue(tracker.request({"id": 7, "result": {}}))
        self.assertTrue(tracker.request({"id": "7", "method": "tools/call"}))
        self.assertEqual(tracker.ids(), ["7"])

    def test_duplicate_with_non_tool_request_is_never_cancelled(self):
        tracker = ObservedToolCalls()
        tracker.request({"id": 2, "method": "tools/list"})
        tracker.request({"id": 2, "method": "tools/call"})
        self.assertEqual(tracker.ids(), [])
        tracker.response({"id": 2, "result": {}})
        tracker.request({"id": 2, "method": "tools/call"})
        self.assertEqual(tracker.ids(), [])

    def test_ambiguous_invalid_ids_stop_further_tracking(self):
        for invalid in (True, 1.0, [], {}):
            with self.subTest(invalid=invalid):
                tracker = ObservedToolCalls()
                tracker.request({"id": 1, "method": "tools/call"})
                tracker.request({"id": invalid, "method": "tools/call"})
                tracker.request({"id": 2, "method": "tools/call"})
                self.assertEqual(tracker.ids(), [])
        tracker = ObservedToolCalls()
        tracker.request({"id": 1, "method": "tools/call"})
        tracker.invalidate()
        tracker.request({"id": 2, "method": "tools/call"})
        self.assertEqual(tracker.ids(), [])
        for invalid in (True, 1.0, [], {}):
            with self.subTest(response_id=invalid):
                tracker = ObservedToolCalls()
                tracker.request({"id": 1, "method": "tools/call"})
                tracker.response({"id": invalid, "result": {}})
                self.assertEqual(tracker.ids(), [])
