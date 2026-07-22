"""Chat SSE + Side-Channel. Spec Section 3.2, 3.3."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from ulid import ULID

from models.session import ChatRequest, HITLRequest
from services.side_channel import stream_relay

router = APIRouter(prefix="/api/agents", tags=["chat"])


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
    actual_agent_id = agent_id

    if not runtime or runtime.get("status") not in ("active", "READY"):
        supervisor_runtime = db.get_runtime_status("supervisor-001")
        if not supervisor_runtime:
            raise HTTPException(status_code=503, detail="No active runtime")
        runtime = supervisor_runtime
        actual_agent_id = "supervisor-001"

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
    item = {
        "PK": f"SESSION#{session_id}",
        "SK": f"HITL#{str(ULID())}",
        "action": req.action,
        "agentId": agent_id,
        "status": "approved" if req.approved else "rejected",
        "resolvedBy": req.resolvedBy,
    }
    db.table.put_item(Item=item)
    return {"sessionId": session_id, "status": item["status"]}
