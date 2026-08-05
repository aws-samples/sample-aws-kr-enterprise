"""Session/Task History API. Agent 수행 이력 + trajectory 조회."""

from fastapi import APIRouter, Depends, Request
from ulid import ULID

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def get_db(request: Request):
    return request.app.state.db_service


def _ulid_to_iso(ulid_str: str) -> str:
    """Decode the millisecond timestamp embedded in a ULID to an ISO 8601
    string. Returns "" if the value is not a valid ULID (so consumers doing
    new Date(...) get a real time, not the opaque id)."""
    try:
        return ULID.from_str(ulid_str).datetime.isoformat()
    except (ValueError, AttributeError):
        return ""


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
        event_id = evt.get("SK", "").replace("EVENT#", "")
        spans.append(
            {
                "eventId": event_id,
                "type": evt.get("type", "unknown"),
                "agentId": evt.get("agentId", ""),
                "data": evt.get("data", {}),
                # SK is EVENT#<ulid>; decode the ULID's embedded time into ISO
                # 8601 instead of returning the opaque id as a "timestamp".
                "timestamp": _ulid_to_iso(event_id[:26]),
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
