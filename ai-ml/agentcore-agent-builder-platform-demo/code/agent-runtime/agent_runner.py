"""Agent Base Image Entrypoint. Spec Section 11.
Lazy initialization: FastAPI starts immediately (< 5s), Agent initializes on first request."""

import json
import os
import logging
import time
import threading
from decimal import Decimal
import boto3
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn

from config_loader import load_config
from side_channel import SideChannelWriter
from harness import Tier2Harness
from internal_tools import create_internal_tool, _request_context
from observability_hook import ObservabilityHook
from opentelemetry import trace
from span_filter import AgentSpanProcessor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AGENT_ID = os.environ.get("AGENT_ID", "")
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "aiops-platform")
REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

# Runtime state -- populated lazily on first request
_state: dict = {}
_init_lock = threading.Lock()
_initialized = False


def _write_debug_event(table, event_type: str, data: dict):
    """초기화 디버그 이벤트를 Side-Channel에 기록."""
    from ulid import ULID

    try:
        item = json.loads(
            json.dumps(
                {
                    "PK": f"SESSION#__debug_{AGENT_ID}",
                    "SK": f"EVENT#{ULID()}",
                    "type": event_type,
                    "data": data,
                    "agentId": AGENT_ID,
                    "expiresAt": int(time.time()) + 86400,
                },
                default=str,
            ),
            parse_float=Decimal,
        )
        table.put_item(Item=item)
    except Exception:
        pass


def _initialize_agent():
    """Lazy agent initialization — called once on first /invocations request."""
    global _initialized
    if _initialized:
        return

    with _init_lock:
        if _initialized:
            return

        logger.info("=== Lazy Agent Initialization Start ===")
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.Table(TABLE_NAME)

        _write_debug_event(table, "init_start", {"agent": AGENT_ID, "ts": time.time()})

        # Config 로드
        for attempt in range(1, 4):
            try:
                config = load_config(AGENT_ID, table)
                break
            except Exception as exc:
                if attempt == 3:
                    _write_debug_event(
                        table, "init_error", {"phase": "config_load", "error": str(exc)}
                    )
                    raise
                time.sleep(2**attempt)

        logger.info("Loaded config for %s: %s", AGENT_ID, config["name"])

        # delegations → scoped_agent_invoke 자동 변환
        all_tool_configs = list(config.get("internalTools", []))
        for deleg in config.get("delegations", []):
            target = deleg.get("targetAgent", "")
            purpose = deleg.get("purpose", "")
            if target:
                all_tool_configs.append(
                    {
                        "name": f"delegate_{target.replace('-', '_')}",
                        "description": f"Delegate to {target}: {purpose}",
                        "type": "scoped_agent_invoke",
                        "targetAgent": target,
                    }
                )

        # internalTools 등록
        internal_tools = []
        tool_errors = []
        for tool_config in all_tool_configs:
            try:
                tool_fn = create_internal_tool(tool_config, dynamodb)
                internal_tools.append(tool_fn)
            except Exception as e:
                tool_errors.append({"name": tool_config.get("name"), "error": str(e)})
                logger.warning(
                    "Skipping internal tool %s: %s", tool_config.get("name"), e
                )

        _write_debug_event(
            table,
            "init_tools",
            {
                "registered": [
                    getattr(t, "tool_name", t.__name__) for t in internal_tools
                ],
                "errors": tool_errors,
                "ts": time.time(),
            },
        )
        logger.info("Registered %d internal tools", len(internal_tools))

        # MCP Gateway — gateways가 비어있으면 skip
        mcp_clients = []
        gateways = config.get("gateways", [])
        if gateways:
            try:
                from mcp_connector import connect_gateways

                mcp_clients = connect_gateways(config)
                logger.info("Connected to %d gateways", len(mcp_clients))
            except BaseException as e:
                logger.warning("MCP Gateway connection skipped: %s", e)

        # Strands Agent with ObservabilityHook
        from strands import Agent
        from strands.models.bedrock import BedrockModel

        model_id = config.get("model", "apac.anthropic.claude-sonnet-4-20250514-v1:0")
        model = BedrockModel(
            model_id=model_id,
            region_name=REGION,
            max_tokens=32768,
        )

        is_supervisor = "supervisor" in AGENT_ID.lower()
        obs_hook = ObservabilityHook(is_supervisor=is_supervisor)

        all_tools: list = list(internal_tools) + list(mcp_clients)

        harness = Tier2Harness(config)
        logger.info(
            "Harness initialized: pre=%s, post=%s",
            harness.pre_hooks,
            harness.post_hooks,
        )

        system_prompt = (
            config.get("systemPrompt", "") or "You are a helpful AI assistant."
        )

        # persona-injection pre-hook: contextBoundary 기반 scope 거부 규칙 자동 주입
        if "persona-injection" in harness.pre_hooks:
            boundary = config.get("contextBoundary", "")
            if boundary:
                scope_rule = (
                    f"\n\n[SCOPE ENFORCEMENT] Your Context Boundary is: {boundary}. "
                    f"You MUST only handle requests within this boundary. "
                    f"For any request outside this boundary, do NOT delegate to other agents "
                    f"and do NOT use any tools. Instead, respond: "
                    f"'This request is outside my scope ({boundary}). "
                    f"Please direct it to the appropriate agent.'"
                )
                system_prompt = system_prompt + scope_rule
                logger.info("Persona-injection: scope rule injected for %s", AGENT_ID)

        agent = Agent(
            model=model,
            tools=all_tools if all_tools else None,
            system_prompt=system_prompt,
            hooks=[obs_hook],
        )

        _state["dynamodb"] = dynamodb
        _state["table"] = table
        _state["agent"] = agent
        _state["harness"] = harness
        _state["config"] = config
        _state["obs_hook"] = obs_hook

        _initialized = True

        _write_debug_event(
            table,
            "init_complete",
            {
                "agent": AGENT_ID,
                "model": model_id,
                "tools_count": len(all_tools),
                "tool_names": [
                    getattr(t, "tool_name", getattr(t, "__name__", str(t)))
                    for t in all_tools
                ],
                "mcp_count": len(mcp_clients),
                "is_supervisor": is_supervisor,
                "ts": time.time(),
            },
        )
        logger.info("=== Agent Initialization Complete ===")

        # OTEL SpanProcessor 등록 — 모든 span에 agent.id, session.id, phase 자동 주입
        try:
            from opentelemetry.sdk.trace import TracerProvider

            tracer_provider = trace.get_tracer_provider()
            if isinstance(tracer_provider, TracerProvider):
                tracer_provider.add_span_processor(AgentSpanProcessor())
                logger.info("AgentSpanProcessor registered")
        except Exception as e:
            logger.warning("SpanProcessor registration skipped: %s", e)


async def _stream_agent(body: dict):
    """Strands stream_async → SSE 이벤트 변환."""
    prompt = body.get("prompt", "")
    context = body.get("context", {})
    session_id = context.get("sessionId", "")

    agent = _state["agent"]
    obs_hook: ObservabilityHook = _state["obs_hook"]
    table = _state["table"]

    _request_context["session_id"] = session_id

    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("session.id", session_id)
        current_span.set_attribute("agent.id", AGENT_ID)

    writer = None
    if session_id:
        writer = SideChannelWriter(
            table=table, session_id=session_id, agent_id=AGENT_ID
        )
        writer.write_event("agent_start", {"agent": AGENT_ID, "mode": "streaming"})
    obs_hook.writer = writer

    try:
        async for event in agent.stream_async(prompt):
            if "data" in event:
                yield f"event: text\ndata: {json.dumps({'content': event['data']})}\n\n"
            elif "current_tool_use" in event and event["current_tool_use"].get("name"):
                tool = event["current_tool_use"]
                yield f"event: tool_call\ndata: {json.dumps({'tool': tool['name'], 'phase': 'start'})}\n\n"
            elif "result" in event:
                result_text = str(event["result"])
                if writer:
                    writer.write_event(
                        "message", {"content": result_text, "final": True}
                    )
                yield f"event: done\ndata: {json.dumps({'content': result_text})}\n\n"
    except Exception as e:
        logger.error("Stream agent error: %s", e)
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
    finally:
        obs_hook.writer = None


app = FastAPI()


@app.post("/invocations")
async def invocations(request: Request):
    # Lazy init on first request
    _initialize_agent()

    body = await request.json()

    # Wake-up call: LLM 호출 없이 초기화만 완료하고 즉시 리턴
    if body.get("type") == "wake-up":
        logger.info("Wake-up call received for agent=%s — ready", AGENT_ID)
        return {"status": "ready", "agent": AGENT_ID}

    # Streaming 분기: Accept 헤더로 결정
    accept = request.headers.get("accept", "application/json")
    if accept == "text/event-stream":
        return StreamingResponse(
            _stream_agent(body),
            media_type="text/event-stream",
        )

    prompt = body.get("prompt", "")
    context = body.get("context", {})
    session_id = context.get("sessionId", "")
    caller = context.get("caller", "")
    depth = context.get("delegationDepth", 0)

    agent = _state["agent"]
    harness = _state["harness"]
    table = _state["table"]
    obs_hook: ObservabilityHook = _state["obs_hook"]

    # A2A tool에 session_id 주입 (LLM이 빈 문자열로 보내도 fallback)
    _request_context["session_id"] = session_id

    current_span = trace.get_current_span()
    if current_span.is_recording():
        current_span.set_attribute("session.id", session_id)
        current_span.set_attribute("agent.id", AGENT_ID)
        current_span.set_attribute("caller", caller)
        current_span.set_attribute("delegation.depth", depth)

    # Tier 2 Pre-Hook: depth 검증
    if not harness.check_depth(depth):
        return {
            "error": f"Delegation depth {depth} exceeds maximum {harness.max_depth}"
        }

    # Debug: messages 상태 로깅 (invoke 전)
    msg_count = len(agent.messages)
    msg_total_chars = sum(len(str(m.get("content", ""))) for m in agent.messages)
    logger.info(
        "PRE_INVOKE agent=%s session=%s messages_count=%d messages_total_chars=%d prompt_len=%d caller=%s depth=%d",
        AGENT_ID,
        session_id[:20] if session_id else "",
        msg_count,
        msg_total_chars,
        len(prompt),
        caller,
        depth,
    )

    # Side-Channel writer 초기화
    writer = None
    if session_id:
        writer = SideChannelWriter(
            table=table, session_id=session_id, agent_id=AGENT_ID
        )
        writer.write_event(
            "agent_start", {"agent": AGENT_ID, "depth": depth, "caller": caller}
        )

    # ObservabilityHook에 writer 주입 (요청별)
    obs_hook.writer = writer

    try:
        result = agent(prompt)
        response_text = str(result)

        # Debug: invoke 결과 로깅
        post_msg_count = len(agent.messages)
        logger.info(
            "POST_INVOKE agent=%s session=%s messages_count=%d response_len=%d stop_reason=%s",
            AGENT_ID,
            session_id[:20] if session_id else "",
            post_msg_count,
            len(response_text),
            getattr(result, "stop_reason", "unknown"),
        )

        if writer:
            writer.write_event("message", {"content": response_text, "final": True})

        return {"response": response_text}
    except Exception as e:
        import traceback

        error_detail = traceback.format_exc()
        logger.error("Agent execution error: %s\n%s", e, error_detail)
        if writer:
            writer.write_event(
                "error", {"message": str(e), "traceback": error_detail[-500:]}
            )
        return {"error": str(e), "traceback": error_detail[-500:]}
    finally:
        obs_hook.writer = None


@app.get("/health")
async def health():
    return {"status": "healthy", "agent": AGENT_ID}


@app.get("/ping")
async def ping():
    import time

    return {"status": "Healthy", "time_of_last_update": int(time.time())}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
