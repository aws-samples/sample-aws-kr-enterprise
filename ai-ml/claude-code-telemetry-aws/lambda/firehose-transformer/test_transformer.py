"""Unit tests for the Firehose transformer's parse_otlp_log.

Run from repo root:
    python3 -m unittest lambda.firehose-transformer.test_transformer -v
or from the lambda dir:
    cd lambda/firehose-transformer && python3 -m unittest test_transformer -v

Fixtures are real OTLP log records captured from CloudWatch Logs on 2026-06-01/02.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from index import parse_otlp_log  # noqa: E402


def _msg(attributes, body, resource=None):
    return json.dumps({
        "body": body,
        "attributes": attributes,
        "resource": resource or {
            "host.arch": "arm64", "os.type": "darwin", "os.version": "25.5.0",
            "service.name": "claude-code", "service.version": "2.1.159",
            "user.name": "testuser",
        },
    })


class TestNewEvents(unittest.TestCase):
    def test_hook_execution_complete(self):
        m = _msg({
            "event.name": "hook_execution_complete", "event.sequence": 32,
            "hook_event": "PreToolUse", "hook_name": "PreToolUse:Workflow",
            "hook_source": "merged", "num_hooks": "1", "num_success": "1",
            "num_blocking": "0", "num_cancelled": "0", "num_non_blocking_error": "0",
            "total_duration_ms": "2", "session.id": "s1",
            "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.hook_execution_complete")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["event_name"], "claude_code.hook_execution_complete")
        self.assertEqual(r["hook_name"], "PreToolUse:Workflow")
        self.assertEqual(r["hook_event"], "PreToolUse")
        self.assertEqual(r["total_duration_ms"], 2.0)
        self.assertEqual(r["num_success"], 1)
        self.assertEqual(r["num_blocking"], 0)
        self.assertEqual(r["event_sequence"], 32)

    def test_plugin_loaded(self):
        m = _msg({
            "event.name": "plugin_loaded", "event.sequence": 1,
            "plugin.name": "skill-creator", "plugin.scope": "official",
            "marketplace.name": "claude-plugins-official", "enabled_via": "user-install",
            "has_hooks": False, "has_mcp": False, "skill_path_count": 1,
            "session.id": "s1", "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.plugin_loaded")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["plugin_name"], "skill-creator")
        self.assertEqual(r["plugin_scope"], "official")
        self.assertEqual(r["marketplace_name"], "claude-plugins-official")
        self.assertEqual(r["enabled_via"], "user-install")
        self.assertEqual(r["has_mcp"], False)

    def test_mcp_server_connection(self):
        m = _msg({
            "event.name": "mcp_server_connection", "event.sequence": 19,
            "duration_ms": "6261", "is_plugin": True, "plugin.name": "playwright",
            "server_scope": "dynamic", "status": "connected", "transport_type": "stdio",
            "session.id": "s1", "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.mcp_server_connection")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["mcp_status"], "connected")
        self.assertEqual(r["transport_type"], "stdio")
        self.assertEqual(r["server_scope"], "dynamic")
        self.assertEqual(r["is_plugin"], True)
        self.assertEqual(r["plugin_name"], "playwright")
        self.assertEqual(r["duration_ms"], 6261.0)

    def test_subagent_completed(self):
        m = _msg({
            "event.name": "subagent_completed", "event.sequence": 5454,
            "agent_type": "general-purpose", "agent.source": "built-in",
            "is_built_in": True, "is_async": False, "duration_ms": 43335,
            "total_tokens": 26821, "total_tool_uses": 6,
            "model": "us.anthropic.claude-opus-4-8[1m]",
            "session.id": "s1", "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.subagent_completed")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["agent_type"], "general-purpose")
        self.assertEqual(r["agent_source"], "built-in")
        self.assertEqual(r["is_built_in"], True)
        self.assertEqual(r["is_async"], False)
        self.assertEqual(r["total_tokens"], 26821)
        self.assertEqual(r["total_tool_uses"], 6)
        self.assertEqual(r["duration_ms"], 43335.0)

    def test_skill_activated(self):
        m = _msg({
            "event.name": "skill_activated", "event.sequence": 18386,
            "skill.name": "frontend-design:frontend-design", "skill.source": "plugin",
            "invocation_trigger": "nested-skill", "plugin.name": "frontend-design",
            "marketplace.name": "claude-plugins-official",
            "session.id": "s1", "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.skill_activated")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["skill_name"], "frontend-design:frontend-design")
        self.assertEqual(r["skill_source"], "plugin")
        self.assertEqual(r["invocation_trigger"], "nested-skill")

    def test_hook_registered(self):
        m = _msg({
            "event.name": "hook_registered", "event.sequence": 11,
            "hook_event": "SessionStart", "hook_source": "flagSettings",
            "hook_type": "command", "session.id": "s1", "user.id": "u1",
            "terminal.type": "ghostty",
        }, "claude_code.hook_registered")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["hook_type"], "command")
        self.assertEqual(r["hook_event"], "SessionStart")


class TestExtendedExistingEvents(unittest.TestCase):
    def test_api_request_new_attrs(self):
        m = _msg({
            "event.name": "api_request", "event.sequence": 3922,
            "model": "claude-opus-4-8", "cost_usd": 4.1023575, "cost_usd_micros": 4102358,
            "duration_ms": 86691, "input_tokens": 19, "output_tokens": 3432,
            "cache_read_tokens": 0, "cache_creation_tokens": 642634,
            "effort": "xhigh", "query_source": "repl_main_thread", "speed": "normal",
            "session.id": "s1", "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.api_request")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["effort"], "xhigh")
        self.assertEqual(r["query_source"], "repl_main_thread")
        self.assertEqual(r["cost_usd_micros"], 4102358)
        self.assertEqual(r["cost_usd"], 4.1023575)  # regression: existing field intact

    def test_api_request_subagent_attribution(self):
        m = _msg({
            "event.name": "api_request", "model": "claude-sonnet-4-6",
            "cost_usd": 0.01, "agent.name": "Explore", "effort": "high",
            "mcp_server.name": "playwright", "mcp_tool.name": "browser_click",
            "session.id": "s1", "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.api_request")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["agent_name"], "Explore")
        self.assertEqual(r["mcp_server_name"], "playwright")
        self.assertEqual(r["mcp_tool_name"], "browser_click")

    def test_tool_result_decision_type_fallback(self):
        # 2.x renamed decision->decision_type, source->decision_source
        m = _msg({
            "event.name": "tool_result", "tool_name": "Bash", "success": True,
            "duration_ms": 1500, "decision_type": "accept", "decision_source": "user",
            "error_type": None, "tool_input_size_bytes": 240, "tool_use_id": "tu_1",
            "session.id": "s1", "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.tool_result")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["decision"], "accept")  # via decision_type fallback
        self.assertEqual(r["source"], "user")       # via decision_source fallback
        self.assertEqual(r["tool_input_size_bytes"], 240)
        self.assertEqual(r["tool_use_id"], "tu_1")

    def test_user_prompt_command_attrs(self):
        m = _msg({
            "event.name": "user_prompt", "prompt_length": "101", "prompt": "<REDACTED>",
            "command_name": "effort", "command_source": "user",
            "session.id": "s1", "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.user_prompt")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["command_name"], "effort")
        self.assertEqual(r["command_source"], "user")


class TestRegression(unittest.TestCase):
    def test_legacy_tool_result_still_works(self):
        # Old 1.x key names must still map
        m = _msg({
            "event.name": "tool_result", "tool_name": "Read", "success": True,
            "duration_ms": 50, "decision": "accept", "source": "config",
            "session.id": "s1", "user.id": "u1", "terminal.type": "ghostty",
        }, "claude_code.tool_result")
        r = parse_otlp_log(m, 1234567890000)
        assert r is not None
        self.assertEqual(r["decision"], "accept")
        self.assertEqual(r["source"], "config")

    def test_non_json_returns_none(self):
        self.assertIsNone(parse_otlp_log("not json", None))

    def test_missing_event_name_returns_none(self):
        self.assertIsNone(parse_otlp_log(json.dumps({"attributes": {"foo": "bar"}}), None))


if __name__ == "__main__":
    unittest.main()
