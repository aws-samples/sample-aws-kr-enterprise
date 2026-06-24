import time
from collections import deque
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger()


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerConfig:
    def __init__(
        self,
        failure_threshold: int = 5,
        failure_window_seconds: float = 60.0,
        cooldown_seconds: float = 120.0,
        success_threshold: int = 3,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.failure_window_seconds = failure_window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.success_threshold = success_threshold


BEDROCK_CONFIG = CircuitBreakerConfig(
    failure_threshold=5,
    failure_window_seconds=60.0,
    cooldown_seconds=120.0,
    success_threshold=3,
)

DYNAMODB_CONFIG = CircuitBreakerConfig(
    failure_threshold=10,
    failure_window_seconds=30.0,
    cooldown_seconds=30.0,
    success_threshold=3,
)


class CircuitBreaker:
    def __init__(self, name: str, config: CircuitBreakerConfig) -> None:
        self._name = name
        self._config = config
        self._state = CircuitState.CLOSED
        self._failures: deque[float] = deque()
        self._consecutive_successes = 0
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._config.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                self._consecutive_successes = 0
                logger.info("circuit_breaker_half_open", name=self._name)
        return self._state

    def is_call_permitted(self) -> bool:
        return self.state != CircuitState.OPEN

    def record_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self._config.success_threshold:
                self._state = CircuitState.CLOSED
                self._failures.clear()
                logger.info("circuit_breaker_closed", name=self._name)
        elif self._state == CircuitState.CLOSED:
            pass

    def record_failure(self) -> None:
        now = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._last_failure_time = now
            logger.warning("circuit_breaker_reopened", name=self._name)
            return

        self._failures.append(now)
        cutoff = now - self._config.failure_window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

        if len(self._failures) >= self._config.failure_threshold:
            self._state = CircuitState.OPEN
            self._last_failure_time = now
            logger.warning(
                "circuit_breaker_opened",
                name=self._name,
                failure_count=len(self._failures),
            )

    def get_status(self) -> dict[str, Any]:
        return {
            "name": self._name,
            "state": self.state,
            "recent_failures": len(self._failures),
            "consecutive_successes": self._consecutive_successes,
        }


bedrock_circuit = CircuitBreaker("bedrock", BEDROCK_CONFIG)
dynamodb_circuit = CircuitBreaker("dynamodb", DYNAMODB_CONFIG)
