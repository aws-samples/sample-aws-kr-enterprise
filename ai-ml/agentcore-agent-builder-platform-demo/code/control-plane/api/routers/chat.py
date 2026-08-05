"""Chat SSE + Side-Channel. Spec Section 3.2, 3.3."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from ulid import ULID

from models.session import ChatRequest, HITLRequest
from services.side_channel import stream_relay

router = APIRouter(prefix="/api/agents", tags=["chat"])

SUPERVISOR_AGENT_ID = "supervisor-001"


def _sse_error(code: str, message: str, hint: str = "") -> StreamingResponse:
    """Return a well-formed SSE stream carrying an explicit error event.

    Used instead of a hard HTTP 503 so the Playground keeps its streaming
    connection and can render a friendly, actionable message to the user
    (the frontend listens for `event: error`).
    """

    def _gen():
        payload = {"error": message, "code": code}
        if hint:
            payload["hint"] = hint
        yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def get_db(request: Request):
    return request.app.state.db_service


def get_agentcore(request: Request):
    return request.app.state.agentcore_client


@router.post("/{agent_id}/chat")
async def start_chat(
    agent_id: str,
    req: ChatRequest,
    db=Depends(get_db),
    ac=Depends(get_agentcore),
):
    session_id = req.sessionId or str(ULID())

    runtime = db.get_runtime_status(agent_id)
    is_ready = bool(runtime) and runtime.get("status") in ("active", "READY")

    # Route to the requested agent's own runtime when it is ready. Do NOT
    # silently substitute the supervisor for a domain agent — that would make
    # the Cost/Incident/etc. playground answer with the supervisor's identity
    # and tools. Instead surface an explicit, actionable error to the client
    # (over SSE, so the Playground stream stays intact) unless the supervisor
    # itself was the requested target.
    if not is_ready:
        if agent_id == SUPERVISOR_AGENT_ID:
            return _sse_error(
                "supervisor_unavailable",
                "The supervisor runtime is not ready yet.",
                "Wait for deployment to finish (status READY), then retry.",
            )
        return _sse_error(
            "agent_not_ready",
            f"Agent '{agent_id}' is not deployed or its runtime is not ready.",
            "Open the agent's Design page and Deploy it, then wait until its "
            "runtime status is READY before using the Playground.",
        )

    runtime_arn = runtime.get("runtimeArn", "")

    db.create_session(session_id, agent_id)

    return StreamingResponse(
        stream_relay(
            agentcore_client=ac,
            runtime_arn=runtime_arn,
            prompt=req.message,
            session_id=session_id,
            context={
                "sessionId": session_id,
                "caller": "platform-api",
                "delegationDepth": 0,
            },
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{agent_id}/feedback/{session_id}")
async def submit_feedback(
    agent_id: str,
    session_id: str,
    req: HITLRequest,
    db=Depends(get_db),
):
    """Record human feedback (approve/reject + comment) for a session.

    This is recorded for audit/review only — the platform does NOT implement a
    runtime approval gate, so this feedback does not pause, block, or roll back
    any agent action. It simply persists the reviewer's decision.
    """
    item = {
        "PK": f"SESSION#{session_id}",
        "SK": f"HITL#{str(ULID())}",
        "agentId": agent_id,
        "status": "approved" if req.approved else "rejected",
        "comment": req.comment,
        "action": req.action,
        "resolvedBy": req.resolvedBy,
    }
    db.table.put_item(Item=item)
    return {"sessionId": session_id, "status": item["status"], "recorded": True}
