"""Custom OTEL SpanProcessor — 비즈니스 context enrichment.

aws-opentelemetry-distro의 auto-instrumentation과 함께 동작하여,
Agent Runtime의 모든 span에 agent_id, session_id 등 비즈니스 context를 주입한다.

noise HTTP span(GET/PUT)은 OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=urllib3,requests,httpx
환경변수로 제거한다 (Dockerfile에 설정)."""

import logging
import os

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

logger = logging.getLogger(__name__)

AGENT_ID = os.environ.get("AGENT_ID", "")


def _get_session_id() -> str:
    """현재 요청의 session_id를 가져온다 (internal_tools._request_context 참조)."""
    try:
        from internal_tools import _request_context

        return _request_context.get("session_id", "")
    except ImportError:
        return ""


class AgentSpanProcessor(SpanProcessor):
    """Agent Runtime span enrichment processor.

    모든 span에 다음 attribute를 자동 주입:
    - agent.id: 현재 Agent ID
    - session.id: 현재 요청의 session ID (있을 경우)
    - agent.phase: span name 기반 phase 분류 (init/tool/chat/io)
    """

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if not span.is_recording():
            return

        if AGENT_ID:
            span.set_attribute("agent.id", AGENT_ID)

        session_id = _get_session_id()
        if session_id:
            span.set_attribute("session.id", session_id)

        phase = self._classify_phase(span.name)
        if phase:
            span.set_attribute("agent.phase", phase)

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    @staticmethod
    def _classify_phase(name: str) -> str:
        """Span name으로 비즈니스 phase를 분류."""
        lower = name.lower()
        if "invoke_agent" in lower:
            return "agent_invoke"
        if "execute_tool" in lower:
            return "tool_execution"
        if "chat" in lower:
            return "llm_call"
        if "dynamodb" in lower:
            return "data_io"
        if "execute_event_loop" in lower:
            return "agent_loop"
        return ""
