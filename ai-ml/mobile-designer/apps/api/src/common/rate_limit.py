"""Lightweight in-memory rate limiter for abuse-sensitive endpoints.

This guards against brute-force on auth endpoints (login, password reset). It is a
per-process sliding window; with multiple ECS tasks each enforces its own window, so
the effective global limit scales with task count. For strict global limits, pair
this with an edge rate limit (AWS WAF / ALB). It is dependency-free by design.
"""

import time
from collections import defaultdict, deque

from fastapi import Request

from src.common.exceptions import AppException


class RateLimitExceededException(AppException):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            status_code=429,
            code="RATE_LIMITED",
            message="Too many requests. Please try again later.",
            details={"retry_after_seconds": retry_after_seconds},
        )


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self._window
        hits = self._hits[key]
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self._max:
            retry_after = int(self._window - (now - hits[0])) + 1
            raise RateLimitExceededException(retry_after)
        hits.append(now)


def _client_ip(request: Request) -> str:
    # Behind CloudFront/ALB the real client IP is the first X-Forwarded-For entry.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# 5 attempts per minute per client IP for authentication-sensitive endpoints.
_auth_limiter = RateLimiter(max_requests=5, window_seconds=60.0)


def auth_rate_limit(request: Request) -> None:
    """FastAPI dependency: throttle auth-sensitive endpoints per client IP."""
    _auth_limiter.check(_client_ip(request))
