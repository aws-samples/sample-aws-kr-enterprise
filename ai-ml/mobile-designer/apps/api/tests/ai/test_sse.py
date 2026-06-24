from src.ai.models import SSEEvent, SSEEventType
from src.ai.sse import _format_event, create_error_event, create_ping_event


class TestSSEFormatting:
    def test_format_basic_event(self) -> None:
        event = SSEEvent(event=SSEEventType.START, data={"message": "hello"})
        result = _format_event(event)
        assert result["event"] == "start"
        assert '"message": "hello"' in result["data"]

    def test_format_event_with_id(self) -> None:
        event = SSEEvent(event=SSEEventType.PROGRESS, data={"step": "analyzing"}, id="evt-123")
        result = _format_event(event)
        assert result["id"] == "evt-123"

    def test_format_event_without_id(self) -> None:
        event = SSEEvent(event=SSEEventType.DONE, data={})
        result = _format_event(event)
        assert "id" not in result

    def test_create_ping_event(self) -> None:
        event = create_ping_event()
        assert event.event == SSEEventType.PING
        assert event.data["type"] == "keepalive"

    def test_create_error_event(self) -> None:
        event = create_error_event("something went wrong")
        assert event.event == SSEEventType.ERROR
        assert event.data["message"] == "something went wrong"

    def test_unicode_data(self) -> None:
        event = SSEEvent(event=SSEEventType.PROGRESS, data={"message": "디자인 생성 중"})
        result = _format_event(event)
        assert "디자인 생성 중" in result["data"]
