from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class AppException(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class NotFoundException(AppException):
    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            status_code=404,
            code="NOT_FOUND",
            message=f"{resource} not found: {identifier}",
        )


class ConflictException(AppException):
    def __init__(self, message: str) -> None:
        super().__init__(status_code=409, code="CONFLICT", message=message)


class UnauthorizedException(AppException):
    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(status_code=401, code="UNAUTHORIZED", message=message)


class ForbiddenException(AppException):
    def __init__(self, message: str = "Access denied") -> None:
        super().__init__(status_code=403, code="FORBIDDEN", message=message)


class ValidationException(AppException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(status_code=422, code="VALIDATION_ERROR", message=message, details=details)


class ServiceUnavailableException(AppException):
    def __init__(self, service: str) -> None:
        super().__init__(
            status_code=503,
            code="SERVICE_UNAVAILABLE",
            message=f"{service} is temporarily unavailable. Please try again later.",
        )


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )
