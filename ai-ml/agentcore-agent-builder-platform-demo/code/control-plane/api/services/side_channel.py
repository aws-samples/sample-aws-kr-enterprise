"""Side-Channel poller + SSE writer. Spec Section 3.3."""

import asyncio
import json
import logging
import threading
from decimal import Decimal
from typing import AsyncGenerator


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == int(o) else float(o)
        return super().default(o)


logger = logging.getLogger(__name__)

POLL_INTERVAL_MS = 500


async def poll_and_stream(
    db_service,
    agentcore_client,
    runtime_arn: str,
    prompt: str,
    session_id: str,
    context: dict = None,
) -> AsyncGenerator[str, None]:
    """두 개의 비동기 태스크:
    Task A: invoke_agent_runtime() (메인 — 블로킹)
    Task B: DynamoDB 폴링 루프 (사이드 — 500ms 간격)"""

    # Wake-up downstream agents (fire-and-forget)
    agent_id = (context or {}).get("targetAgentId", "")
    if agent_id:
        _schedule_downstream_wakeup(db_service, agentcore_client, agent_id, session_id)

    last_sk = ""
    invoke_done = asyncio.Event()
    final_result = {"response": ""}

    async def invoke_task():
        try:
            result = await asyncio.to_thread(
                agentcore_client.invoke_runtime,
                runtime_arn,
                prompt,
                context,
            )
            final_result["response"] = result
        except Exception as e:
            final_result["response"] = json.dumps({"error": str(e)})
        finally:
            invoke_done.set()

    asyncio.create_task(invoke_task())

    while not invoke_done.is_set():
        events = db_service.poll_session_events(session_id, after_sk=last_sk)
        for event in events:
            last_sk = event["SK"]
            event_type = event.get("type", "status")
            event_data = event.get("data", {})
            if event_type == "message":
                event_data.pop("content", None)
            yield f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False, cls=_DecimalEncoder)}\n\n"

        yield ": keepalive\n\n"
        await asyncio.sleep(POLL_INTERVAL_MS / 1000)

    # invoke 완료 후 남은 이벤트 수집
    events = db_service.poll_session_events(session_id, after_sk=last_sk)
    for event in events:
        event_type = event.get("type", "status")
        event_data = event.get("data", {})
        if event_type == "message":
            event_data.pop("content", None)
        yield f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False, cls=_DecimalEncoder)}\n\n"

    # 최종 결과 — invoke_runtime()이 JSON string을 반환하면 response 필드만 추출
    raw = final_result["response"]
    try:
        parsed = json.loads(raw)
        content = parsed.get("response", raw)
    except (json.JSONDecodeError, TypeError):
        content = raw
    yield f"event: message\ndata: {json.dumps({'content': content}, ensure_ascii=False, cls=_DecimalEncoder)}\n\n"
    yield "event: done\ndata: {}\n\n"


def _schedule_downstream_wakeup(db_service, agentcore_client, agent_id, session_id):
    """Agent의 delegations + Supervisor 레지스트리 조회 → 각 downstream에 wake-up invoke 비동기 전송."""
    target_agent_ids: set[str] = set()

    # 1) delegations 기반
    config = db_service.get_agent_config(agent_id)
    if config:
        for deleg in config.get("delegations", []):
            tid = deleg.get("targetAgent", "")
            if tid:
                target_agent_ids.add(tid)

    # 2) Supervisor 레지스트리 기반 (PK=SUPERVISOR, SK=AGENT#*)
    if "supervisor" in agent_id.lower():
        for entry in db_service.list_supervisor_agents():
            sk = entry.get("SK", "")
            if sk.startswith("AGENT#"):
                target_agent_ids.add(sk.split("#", 1)[1])

    if not target_agent_ids:
        return

    for target_agent_id in target_agent_ids:
        runtime_info = db_service.get_runtime_status(target_agent_id)
        if not runtime_info or runtime_info.get("status") not in ("active", "READY"):
            continue
        target_arn = runtime_info.get("runtimeArn", "")
        if not target_arn:
            continue
        threading.Thread(
            target=_send_wakeup,
            args=(agentcore_client, target_arn, session_id),
            daemon=True,
        ).start()
    logger.info(
        "Wake-up scheduled for %d downstream agents of %s",
        len(target_agent_ids),
        agent_id,
    )


def _send_wakeup(agentcore_client, runtime_arn, session_id):
    """단일 downstream Agent에 wake-up invoke 전송. 실패해도 무시."""
    try:
        payload = json.dumps({"type": "wake-up"}).encode("utf-8")
        invoke_kwargs = {
            "agentRuntimeArn": runtime_arn,
            "contentType": "application/json",
            "accept": "application/json",
            "payload": payload,
        }
        if session_id:
            invoke_kwargs["runtimeSessionId"] = session_id
        agentcore_client.runtime_client.invoke_agent_runtime(**invoke_kwargs)
    except Exception as e:
        logger.warning("Wake-up failed for %s: %s", runtime_arn, e)


async def stream_relay(
    agentcore_client,
    runtime_arn: str,
    prompt: str,
    session_id: str,
    context: dict = None,
) -> AsyncGenerator[str, None]:
    """AgentCore streaming response를 Frontend SSE로 직접 relay.
    DynamoDB 폴링 없이 invoke_runtime_stream()의 SSE 라인을 그대로 전달."""
    try:
        async for line in _iter_stream(agentcore_client, runtime_arn, prompt, context):
            yield f"{line}\n\n"
    except Exception as e:
        logger.error("stream_relay error: %s", e)
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"


async def _iter_stream(agentcore_client, runtime_arn, prompt, context):
    """invoke_runtime_stream() (동기 generator)을 asyncio.to_thread로 래핑."""
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def _produce():
        try:
            for line in agentcore_client.invoke_runtime_stream(runtime_arn, prompt, context):
                loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception as e:
            # Surface the failure as an SSE error event instead of silently
            # ending the stream (which the client can't distinguish from a
            # normal empty completion). Details are logged server-side.
            logger.error("invoke_runtime_stream failed for %s: %s", runtime_arn, e)
            err_line = (
                "event: error\n"
                'data: {"error": "Agent runtime invocation failed", '
                '"code": "runtime_invoke_failed"}'
            )
            loop.call_soon_threadsafe(queue.put_nowait, err_line)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    threading.Thread(target=_produce, daemon=True).start()

    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=30)
        except asyncio.TimeoutError:
            yield ": keepalive"
            continue
        if item is sentinel:
            break
        yield item
