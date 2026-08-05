"""Observability Hook — Strands HookProvider로 Agent 활동을 Side-Channel에 실시간 발행.

BeforeToolCallEvent/AfterToolCallEvent를 구독하여 tool_call 이벤트를 DynamoDB Side-Channel에 기록.
Supervisor의 경우 invoke_domain_agent 호출을 routing 이벤트로도 발행.
AgentCore OpenTelemetry와 병행 사용 가능. Spec Section 3.3, 8."""

from __future__ import annotations

import logging
import time
from typing import Any

from strands.hooks.registry import HookRegistry, HookProvider
from strands.hooks.events import (
    BeforeToolCallEvent,
    AfterToolCallEvent,
)

from opentelemetry import trace
from side_channel import SideChannelWriter

logger = logging.getLogger(__name__)

# Must match the supervisor's routing tool name seeded in scripts/seed-dynamodb.sh
# (supervisor-001 internalTools -> "invoke_domain_agent", type agent_invoke).
# Previously "invoke_agent", which never matched, so routing events were never emitted.
ROUTING_TOOL_NAME = "invoke_domain_agent"


class ObservabilityHook(HookProvider):
    """Agent 실행 중 tool_call 이벤트를 Side-Channel에 발행하는 HookProvider.
    Supervisor의 invoke_agent 호출은 추가로 routing 이벤트를 발행한다."""

    def __init__(
        self, writer: SideChannelWriter | None = None, is_supervisor: bool = False
    ):
        self._writer = writer
        self._is_supervisor = is_supervisor

    @property
    def writer(self) -> SideChannelWriter | None:
        return self._writer

    @writer.setter
    def writer(self, w: SideChannelWriter | None) -> None:
        self._writer = w

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)

    def _on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        if not self._writer:
            return
        tool_name = event.tool_use.get("name", "unknown")

        current_span = trace.get_current_span()
        if current_span.is_recording():
            current_span.set_attribute("gen_ai.tool.name", tool_name)

        tool_input = event.tool_use.get("input", {})

        # Supervisor의 invoke_agent → routing 이벤트 발행
        if self._is_supervisor and tool_name == ROUTING_TOOL_NAME:
            target_agent = (
                tool_input.get("agent_id", "unknown")
                if isinstance(tool_input, dict)
                else "unknown"
            )
            self._writer.write_event(
                "routing",
                {
                    "target": target_agent,
                    "prompt_summary": str(tool_input.get("prompt", ""))[:100]
                    if isinstance(tool_input, dict)
                    else "",
                    "timestamp": time.time(),
                },
            )

        self._writer.write_event(
            "tool_call",
            {
                "tool": tool_name,
                "phase": "start",
                "input_summary": _summarize_input(tool_input),
                "timestamp": time.time(),
            },
        )

    def _on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        if not self._writer:
            return
        tool_name = event.tool_use.get("name", "unknown")
        has_error = event.exception is not None

        data: dict[str, Any] = {
            "tool": tool_name,
            "phase": "end",
            "timestamp": time.time(),
        }
        if has_error:
            data["error"] = str(event.exception)
        else:
            data["result_summary"] = _summarize_result(event.result)

        self._writer.write_event("tool_call", data)


def _summarize_input(tool_input: Any) -> str:
    """Tool input을 짧은 요약 문자열로 변환. 긴 값은 잘라냄."""
    if isinstance(tool_input, dict):
        parts = []
        for k, v in list(tool_input.items())[:5]:
            val = str(v)[:80]
            parts.append(f"{k}={val}")
        return ", ".join(parts)
    return str(tool_input)[:200]


def _summarize_result(result: Any) -> str:
    """Tool result를 짧은 요약 문자열로 변환."""
    if result is None:
        return ""
    text = str(result)
    if len(text) > 200:
        return text[:197] + "..."
    return text
