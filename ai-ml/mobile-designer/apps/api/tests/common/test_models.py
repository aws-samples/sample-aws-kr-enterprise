from src.common.models import ErrorDetail, ErrorResponse, PaginatedResponse, PaginationParams


class TestPaginationParams:
    def test_defaults(self) -> None:
        p = PaginationParams()
        assert p.limit == 20
        assert p.cursor is None

    def test_custom_values(self) -> None:
        p = PaginationParams(limit=50, cursor="abc123")
        assert p.limit == 50
        assert p.cursor == "abc123"


class TestPaginatedResponse:
    def test_no_more_items(self) -> None:
        r = PaginatedResponse(items=["a", "b"], next_cursor=None, has_more=False)
        assert len(r.items) == 2
        assert r.has_more is False

    def test_with_cursor(self) -> None:
        r = PaginatedResponse(items=["x"], next_cursor="next-key", has_more=True)
        assert r.next_cursor == "next-key"


class TestErrorResponse:
    def test_serialization(self) -> None:
        r = ErrorResponse(error=ErrorDetail(code="TEST", message="fail"))
        data = r.model_dump()
        assert data["error"]["code"] == "TEST"
