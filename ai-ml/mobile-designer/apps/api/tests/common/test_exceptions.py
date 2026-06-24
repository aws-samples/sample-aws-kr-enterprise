import pytest
from unittest.mock import MagicMock

from src.common.exceptions import (
    AppException,
    ConflictException,
    ForbiddenException,
    NotFoundException,
    ServiceUnavailableException,
    UnauthorizedException,
    ValidationException,
    app_exception_handler,
)


class TestExceptionHierarchy:
    def test_not_found(self) -> None:
        exc = NotFoundException("Project", "p-123")
        assert exc.status_code == 404
        assert exc.code == "NOT_FOUND"
        assert "p-123" in exc.message

    def test_conflict(self) -> None:
        exc = ConflictException("Email already exists")
        assert exc.status_code == 409
        assert exc.code == "CONFLICT"

    def test_unauthorized(self) -> None:
        exc = UnauthorizedException()
        assert exc.status_code == 401
        assert "Authentication" in exc.message

    def test_forbidden(self) -> None:
        exc = ForbiddenException("No access")
        assert exc.status_code == 403

    def test_validation(self) -> None:
        exc = ValidationException("Bad input", details={"field": "email"})
        assert exc.status_code == 422
        assert exc.details == {"field": "email"}

    def test_service_unavailable(self) -> None:
        exc = ServiceUnavailableException("AI")
        assert exc.status_code == 503
        assert "AI" in exc.message


class TestExceptionHandler:
    @pytest.mark.asyncio
    async def test_handler_returns_json_response(self) -> None:
        request = MagicMock()
        exc = NotFoundException("User", "u-1")
        response = await app_exception_handler(request, exc)
        assert response.status_code == 404
        assert b"NOT_FOUND" in response.body
