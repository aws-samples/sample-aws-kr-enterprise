"""Builder Agent chat. Spec Section 3.2, 4.1."""

import json
import logging

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from ulid import ULID

from models.session import BuilderChatRequest

router = APIRouter(prefix="/api/builder", tags=["builder"])
logger = logging.getLogger(__name__)


def get_builder(request: Request):
    return request.app.state.builder_service


@router.post("/chat")
async def builder_chat(req: BuilderChatRequest, builder=Depends(get_builder)):
    session_id = req.sessionId or str(ULID())

    async def safe_stream():
        try:
            async for event in builder.chat_stream(req.messages, session_id, req.state):
                yield event
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ThrottlingException":
                msg = "요청이 너무 많습니다. 잠시 후 다시 시도해주세요."
            elif error_code == "ModelTimeoutException":
                msg = "모델 응답 시간이 초과되었습니다. 다시 시도해주세요."
            elif error_code == "ValidationException":
                msg = "입력이 너무 깁니다. 대화를 새로 시작해주세요."
            else:
                msg = f"Bedrock 오류: {error_code}"
            logger.error("Builder Bedrock error: %s", e)
            yield f"event: error\ndata: {json.dumps({'error': msg, 'code': error_code}, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error("Builder unexpected error: %s", e)
            yield f"event: error\ndata: {json.dumps({'error': '서버 오류가 발생했습니다. 다시 시도해주세요.'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        safe_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
