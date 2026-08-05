"""Event Trigger endpoint. CloudWatch Alarm -> EventBridge -> Platform API -> Agent invoke."""

import asyncio
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from ulid import ULID

router = APIRouter(prefix="/api/events", tags=["events"])
logger = logging.getLogger(__name__)

# This endpoint is exempt from Cognito JWT auth (called by EventBridge, which
# cannot present a user token). It is instead gated by the shared API-key header
# that the EventBridge connection injects (see modules/compute/eventbridge.tf:
# api_key key=x-api-source). Overridable via EVENT_API_SOURCE for stronger secrets.
EXPECTED_API_SOURCE = os.environ.get("EVENT_API_SOURCE", "eventbridge")


def _verify_event_source(request: Request):
    if request.headers.get("x-api-source") != EXPECTED_API_SOURCE:
        raise HTTPException(status_code=401, detail="Invalid or missing event source")


def get_db(request: Request):
    return request.app.state.db_service


def get_agentcore(request: Request):
    return request.app.state.agentcore_client


@router.post("/alarm")
async def receive_alarm(
    request: Request,
    db=Depends(get_db),
    ac=Depends(get_agentcore),
):
    """CloudWatch Alarm -> EventBridge -> 이 endpoint -> Incident Agent invoke."""
    _verify_event_source(request)
    body = await request.json()
    session_id = str(ULID())

    detail = body.get("detail", body)
    alarm_name = detail.get("alarmName", detail.get("AlarmName", "unknown"))
    alarm_state = detail.get("state", detail.get("newStateValue", {}))
    if isinstance(alarm_state, dict):
        alarm_state_value = alarm_state.get("value", "ALARM")
        alarm_reason = alarm_state.get("reason", "")
    else:
        alarm_state_value = str(alarm_state)
        alarm_reason = detail.get("newStateReason", "")

    source_account = body.get("account", detail.get("account", ""))
    region = body.get("region", detail.get("region", "ap-northeast-2"))

    logger.info(
        "Alarm received: name=%s state=%s account=%s session=%s",
        alarm_name,
        alarm_state_value,
        source_account,
        session_id,
    )

    prompt = (
        f"[AUTO-TRIGGERED] CloudWatch Alarm이 발생했습니다. "
        f"즉시 인시던트를 생성하고 RCA를 시작하세요.\n\n"
        f"Alarm Name: {alarm_name}\n"
        f"State: {alarm_state_value}\n"
        f"Reason: {alarm_reason}\n"
        f"Account: {source_account}\n"
        f"Region: {region}\n\n"
        f"1. 인시던트를 DynamoDB에 기록하세요.\n"
        f"2. RCA Agent에게 위임하여 근본 원인 분석을 시작하세요.\n"
        f"3. 분석 결과를 Report Agent에게 위임하여 리포트를 생성하세요."
    )

    target_agent = "incident-agent-001"
    runtime = db.get_runtime_status(target_agent)
    if not runtime or runtime.get("status") not in ("active", "READY"):
        logger.warning("Incident agent not ready, trying supervisor")
        runtime = db.get_runtime_status("supervisor-001")
        target_agent = "supervisor-001"

    if not runtime or not runtime.get("runtimeArn"):
        logger.error("No agent runtime available for alarm processing")
        return {
            "status": "error",
            "detail": "No agent runtime available",
            "sessionId": session_id,
        }

    runtime_arn = runtime["runtimeArn"]
    db.create_session(session_id, target_agent, trigger="event")

    asyncio.create_task(_invoke_agent_async(ac, runtime_arn, prompt, session_id))

    return {
        "status": "accepted",
        "sessionId": session_id,
        "targetAgent": target_agent,
        "alarmName": alarm_name,
    }


async def _invoke_agent_async(ac, runtime_arn, prompt, session_id):
    """Agent를 비동기로 invoke."""
    try:
        await asyncio.to_thread(
            ac.invoke_runtime,
            runtime_arn,
            prompt,
            {
                "sessionId": session_id,
                "caller": "event-trigger",
                "delegationDepth": 0,
            },
        )
        logger.info("Event-triggered invoke completed for session=%s", session_id)
    except Exception as e:
        logger.error("Event-triggered invoke failed: %s", e)
