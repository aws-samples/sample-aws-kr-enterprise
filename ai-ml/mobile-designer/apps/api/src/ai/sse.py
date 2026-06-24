import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator

from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request

from src.ai.models import SSEEvent, SSEEventType

HEARTBEAT_INTERVAL_SECONDS = 15


async def create_sse_response(
    request: Request,
    event_generator: AsyncGenerator[SSEEvent, None],
) -> EventSourceResponse:
    async def stream() -> AsyncGenerator[dict[str, str], None]:
        heartbeat_task = asyncio.create_task(_heartbeat_generator())

        try:
            async for event in event_generator:
                if await request.is_disconnected():
                    break
                yield _format_event(event)
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    return EventSourceResponse(stream())


async def _heartbeat_generator() -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


def _format_event(event: SSEEvent) -> dict[str, str]:
    result: dict[str, str] = {
        "event": event.event.value,
        "data": json.dumps(event.data, ensure_ascii=False),
    }
    if event.id:
        result["id"] = event.id
    return result


def create_ping_event() -> SSEEvent:
    return SSEEvent(event=SSEEventType.PING, data={"type": "keepalive"})


def create_error_event(message: str) -> SSEEvent:
    return SSEEvent(event=SSEEventType.ERROR, data={"message": message})
