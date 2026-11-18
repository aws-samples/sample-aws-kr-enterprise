import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[1] / "examples" / "agentcore" / "background.py"
SPEC = importlib.util.spec_from_file_location("agentcore_background_example", PATH)
BACKGROUND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKGROUND)
SESSION = "01234567-89ab-cdef-0123-456789abcdef"
TASK = "sdk-task-" + "a" * 32
REQUEST = "b" * 32


class AgentCoreBackgroundCliTests(unittest.TestCase):
    def invoke(self, args, response=None, stream_lines=None):
        calls, clients = [], []
        def send(**kwargs):
            calls.append(kwargs)
            if stream_lines is not None:
                return {"contentType": "text/event-stream",
                        "response": types.SimpleNamespace(iter_lines=lambda: iter(stream_lines))}
            return {"response": io.BytesIO(json.dumps(response or {"status": "ok"}).encode())}
        def client(*args, **kwargs):
            clients.append(kwargs)
            return types.SimpleNamespace(invoke_agent_runtime=send)
        boto = types.SimpleNamespace(Session=lambda **kwargs: types.SimpleNamespace(client=client))
        botocore = types.ModuleType("botocore")
        config = types.ModuleType("botocore.config")
        config.Config = lambda **kwargs: kwargs
        argv = ["background.py", "--region", "us-east-1", "--runtime-arn", "runtime",
                "--session-id", SESSION, *args]
        output = io.StringIO()
        with patch.dict(sys.modules, {"boto3": boto, "botocore": botocore, "botocore.config": config}), \
                patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            BACKGROUND.main()
        return calls, clients, output.getvalue()

    def test_start_uses_reusable_request_id_and_private_endpoint(self):
        with tempfile.TemporaryDirectory() as root:
            prompt = Path(root) / "prompt.txt"
            prompt.write_text("private prompt", encoding="utf-8")
            calls, clients, output = self.invoke([
                "--endpoint-url", "https://vpce-example.bedrock-agentcore.us-east-1.vpce.amazonaws.com",
                "start", "--request-id", REQUEST, "--prompt-file", str(prompt)], {"task_id": TASK})
        self.assertEqual(calls[0]["runtimeSessionId"], SESSION)
        self.assertEqual(json.loads(calls[0]["payload"]), {
            "action": "background_task_start", "request_id": REQUEST, "prompt": "private prompt"})
        self.assertEqual(clients[0]["config"]["retries"]["total_max_attempts"], 1)
        self.assertTrue(clients[0]["endpoint_url"].startswith("https://vpce-"))
        self.assertNotIn("private prompt", output)

    def test_list_and_task_actions_use_expected_payloads(self):
        calls, _, _ = self.invoke(["list", "--all"])
        self.assertEqual(json.loads(calls[0]["payload"]), {"action": "background_tasks", "include_completed": True})
        for action in ("status", "result", "stop", "forget", "archive", "archive-get", "archive-delete",
                       "archive-events", "archive-events-delete", "checkpoint-events", "checkpoint-events-delete"):
            with self.subTest(action=action):
                calls, _, _ = self.invoke([action, TASK])
                self.assertEqual(json.loads(calls[0]["payload"]),
                                 {"action": "background_task_" + action.replace("-", "_"), "task_id": TASK})
        calls, _, _ = self.invoke(["events", TASK, "--after", "9", "--limit", "2"])
        self.assertEqual(json.loads(calls[0]["payload"]),
                         {"action": "background_task_events", "task_id": TASK, "after": 9, "limit": 2})
        calls, _, _ = self.invoke(["archive-events-get", TASK, "--after", "9", "--limit", "2"])
        self.assertEqual(json.loads(calls[0]["payload"]),
                         {"action": "background_task_archive_events_get", "task_id": TASK, "after": 9, "limit": 2})
        calls, _, _ = self.invoke(["checkpoint-events-get", TASK, "--after", "9", "--limit", "2"])
        self.assertEqual(json.loads(calls[0]["payload"]),
                         {"action": "background_task_checkpoint_events_get", "task_id": TASK, "after": 9, "limit": 2})

    def test_retry_requires_acknowledgement_and_sends_distinct_request_id(self):
        calls, _, _ = self.invoke(["retry", "--previous-request-id", "a" * 32, "--request-id", REQUEST,
                                   "--acknowledge-side-effects", "retry prompt"])
        self.assertEqual(json.loads(calls[0]["payload"]), {
            "action": "background_task_retry", "previous_request_id": "a" * 32,
            "request_id": REQUEST, "prompt": "retry prompt", "acknowledge_side_effects": True})
        for args in (["retry", "--previous-request-id", "a" * 32, "--request-id", REQUEST, "hello"],
                     ["retry", "--previous-request-id", REQUEST, "--request-id", REQUEST,
                      "--acknowledge-side-effects", "hello"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke(args)

    def test_follow_streams_pages_and_reports_errors(self):
        calls, _, output = self.invoke(["events", TASK, "--after", "9", "--follow"], stream_lines=[
            b'data: {"type":"events","next_cursor":10}', b'data: {"type":"complete","task":{"state":"completed"}}'])
        self.assertEqual(calls[0]["accept"], "text/event-stream")
        self.assertEqual(json.loads(calls[0]["payload"]),
                         {"action": "background_task_events_stream", "task_id": TASK, "after": 9, "limit": 32})
        self.assertIn('"next_cursor":10', output)
        _, _, output = self.invoke(["events", TASK, "--follow"],
                                   stream_lines=[b'data: {"type":"timeout","next_cursor":10}'])
        self.assertIn('"type":"timeout"', output)
        with self.assertRaises(SystemExit) as error:
            self.invoke(["events", TASK, "--follow"], stream_lines=[b'data: {"type":"error"}'])
        self.assertEqual(error.exception.code, 1)
        with self.assertRaises(SystemExit) as error:
            self.invoke(["events", TASK, "--follow"],
                        stream_lines=[b'data: {"type":"complete","task":{"state":"failed"}}'])
        self.assertEqual(error.exception.code, 1)
        with self.assertRaises(SystemExit) as error:
            self.invoke(["events", TASK, "--follow"],
                        stream_lines=[b'data: {"type":"complete","task":{"state":"cancelled"}}'])
        self.assertEqual(error.exception.code, 1)
        with self.assertRaisesRegex(SystemExit, "without a completion or timeout"):
            self.invoke(["events", TASK, "--follow"], stream_lines=[b'data: {"type":"events","next_cursor":10}'])

    def test_invalid_ids_and_endpoint_are_rejected_before_network(self):
        for args in (["start", "--request-id", "invalid", "hello"],
                     ["status", "../bad"],
                     ["events", TASK, "--after", "-1"],
                     ["archive-events-get", TASK, "--limit", "65"],
                     ["events", TASK, "--limit", "65"],
                     ["--endpoint-url", "http://untrusted.example", "list"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke(args)

    def test_server_error_produces_a_failure_exit(self):
        with self.assertRaises(SystemExit) as error:
            self.invoke(["status", TASK], {"error": "not authorized"})
        self.assertEqual(error.exception.code, 1)

    def test_stop_reports_incomplete_cleanup_as_failure_but_allows_status_inspection(self):
        task = {"task_id": TASK, "state": "failed", "cleanup_pending": True}
        with self.assertRaises(SystemExit) as error:
            self.invoke(["stop", TASK], {"task": task})
        self.assertEqual(error.exception.code, 1)
        self.invoke(["status", TASK], {"task": task})
        _, _, output = self.invoke(["stop", TASK], {"task": {**task, "cleanup_pending": False}})
        self.assertFalse(json.loads(output)["task"]["cleanup_pending"])
