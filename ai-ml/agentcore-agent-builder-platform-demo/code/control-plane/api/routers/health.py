"""Health endpoint used by the ALB platform-api target group.

The ALB uses /health (matcher 200) as its only signal, so it must reflect the
real ability to serve requests: an unconfigured/unreachable DynamoDB table
means every /api/... route 500s, and that must surface as an unhealthy 503 so
the target is drained instead of receiving traffic it cannot serve.
"""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def _get_db(request: Request):
    return getattr(request.app.state, "db_service", None)


@router.get("/health")
async def health(request: Request):
    db = _get_db(request)
    if db is None or getattr(db, "table", None) is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "platform-api",
                "detail": "DynamoDB table not configured",
            },
        )

    try:
        # Cheap, bounded connectivity probe against the real table. Use a
        # get_item on a sentinel key rather than table_status/DescribeTable:
        # the task role is scoped to item-level actions (GetItem/Query/...),
        # NOT dynamodb:DescribeTable, so probing table_status would raise
        # AccessDenied and wrongly fail the health check. A missing item still
        # returns 200 from DynamoDB, which is exactly the "can I serve?" signal.
        await asyncio.to_thread(
            lambda: db.table.get_item(Key={"PK": "_healthcheck", "SK": "_probe"})
        )
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "platform-api",
                "detail": f"DynamoDB unreachable: {type(e).__name__}",
            },
        )

    return {"status": "healthy", "service": "platform-api"}
