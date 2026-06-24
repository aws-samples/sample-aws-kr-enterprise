import time
from unittest.mock import patch

from src.common.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState


class TestCircuitBreaker:
    def _make_breaker(
        self,
        failure_threshold: int = 3,
        failure_window: float = 10.0,
        cooldown: float = 5.0,
        success_threshold: int = 2,
    ) -> CircuitBreaker:
        config = CircuitBreakerConfig(
            failure_threshold=failure_threshold,
            failure_window_seconds=failure_window,
            cooldown_seconds=cooldown,
            success_threshold=success_threshold,
        )
        return CircuitBreaker("test", config)

    def test_starts_closed(self) -> None:
        cb = self._make_breaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.is_call_permitted()

    def test_opens_after_threshold(self) -> None:
        cb = self._make_breaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert not cb.is_call_permitted()

    def test_transitions_to_half_open_after_cooldown(self) -> None:
        cb = self._make_breaker(failure_threshold=2, cooldown=1.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        with patch("time.monotonic", return_value=time.monotonic() + 2.0):
            cb._last_failure_time = time.monotonic() - 2.0
            assert cb.state == CircuitState.HALF_OPEN
            assert cb.is_call_permitted()

    def test_closes_after_success_threshold(self) -> None:
        cb = self._make_breaker(failure_threshold=2, cooldown=0.0, success_threshold=2)
        cb.record_failure()
        cb.record_failure()
        cb._last_failure_time = time.monotonic() - 1.0
        _ = cb.state  # trigger transition to half_open
        cb.record_success()
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self) -> None:
        cb = self._make_breaker(failure_threshold=2, cooldown=60.0)
        cb.record_failure()
        cb.record_failure()
        # Force into HALF_OPEN by manipulating last failure time
        cb._last_failure_time = time.monotonic() - 61.0
        _ = cb.state
        assert cb._state == CircuitState.HALF_OPEN
        cb.record_failure()
        # After failure in HALF_OPEN, should reopen with fresh cooldown
        assert cb._state == CircuitState.OPEN

    def test_failures_expire_outside_window(self) -> None:
        cb = self._make_breaker(failure_threshold=3, failure_window=1.0)
        cb.record_failure()
        cb.record_failure()
        cb._failures[0] = time.monotonic() - 2.0
        cb._failures[1] = time.monotonic() - 2.0
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_get_status(self) -> None:
        cb = self._make_breaker()
        status = cb.get_status()
        assert status["name"] == "test"
        assert status["state"] == CircuitState.CLOSED
        assert status["recent_failures"] == 0
