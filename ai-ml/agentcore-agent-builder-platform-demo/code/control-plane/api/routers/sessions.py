"""Session/Task History API. Agent 수행 이력 + trajectory 조회."""

from fastapi import APIRouter, Depends, Request

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def get_db(request: Request):
    return request.app.state.db_service


@router.get("")
async def list_sessions(limit: int = 20, db=Depends(get_db)):
    """최근 세션 목록."""
    sessions = db.list_recent_sessions(limit=limit)
    return {"sessions": sessions, "count": len(sessions)}


@router.get("/{session_id}")
async def get_session_detail(session_id: str, db=Depends(get_db)):
    """세션 상세: META + 전체 이벤트(trajectory)."""
    meta_resp = db.table.get_item(Key={"PK": f"SESSION#{session_id}", "SK": "META"})
    meta = meta_resp.get("Item", {})
    events = db.get_session_events(session_id)

    spans = []
    for evt in events:
        spans.append(
            {
                "eventId": evt.get("SK", "").replace("EVENT#", ""),
                "type": evt.get("type", "unknown"),
                "agentId": evt.get("agentId", ""),
                "data": evt.get("data", {}),
                "timestamp": evt.get("SK", "").replace("EVENT#", "")[:26],
            }
        )

    return {
        "sessionId": session_id,
        "agentId": meta.get("agentId", ""),
        "trigger": meta.get("trigger", "chat"),
        "startedAt": meta.get("startedAt", ""),
        "status": meta.get("status", ""),
        "spans": spans,
        "spanCount": len(spans),
    }
