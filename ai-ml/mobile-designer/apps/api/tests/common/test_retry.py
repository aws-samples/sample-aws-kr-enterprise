import pytest
from botocore.exceptions import ClientError

from src.common.retry import RetryConfig, _calculate_delay, _is_retryable, retry_with_backoff


class TestIsRetryable:
    def test_retryable_client_error(self) -> None:
        error = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "PutItem",
        )
        assert _is_retryable(error, {"ThrottlingException", "InternalServerError"})

    def test_non_retryable_client_error(self) -> None:
        error = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Bad input"}},
            "PutItem",
        )
        assert not _is_retryable(error, {"ThrottlingException", "InternalServerError"})

    def test_non_client_error(self) -> None:
        error = ValueError("something")
        assert not _is_retryable(error, {"ThrottlingException"})


class TestCalculateDelay:
    def test_delay_within_bounds(self) -> None:
        config = RetryConfig(base_delay_ms=100, max_delay_ms=5000, backoff_factor=4)
        for attempt in range(5):
            delay = _calculate_delay(attempt, config)
            assert 0 <= delay <= config.max_delay_ms / 1000.0

    def test_first_attempt_small_delay(self) -> None:
        config = RetryConfig(base_delay_ms=100, max_delay_ms=5000, backoff_factor=4)
        delays = [_calculate_delay(0, config) for _ in range(100)]
        assert all(d <= 0.1 for d in delays)


class TestRetryWithBackoff:
    @pytest.mark.asyncio
    async def test_success_no_retry(self) -> None:
        call_count = 0

        async def succeed() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        config = RetryConfig(max_attempts=3, base_delay_ms=10)
        result = await retry_with_backoff(succeed, config, {"ThrottlingException"})
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_retryable_error(self) -> None:
        call_count = 0

        async def fail_then_succeed() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
                    "Query",
                )
            return "ok"

        config = RetryConfig(max_attempts=3, base_delay_ms=1)
        result = await retry_with_backoff(fail_then_succeed, config, {"ThrottlingException"})
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_non_retryable_immediately(self) -> None:
        call_count = 0

        async def fail_non_retryable() -> str:
            nonlocal call_count
            call_count += 1
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "bad"}},
                "PutItem",
            )

        config = RetryConfig(max_attempts=3, base_delay_ms=1)
        with pytest.raises(ClientError):
            await retry_with_backoff(fail_non_retryable, config, {"ThrottlingException"})
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_exhausts_retries(self) -> None:
        call_count = 0

        async def always_fail() -> str:
            nonlocal call_count
            call_count += 1
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
                "Query",
            )

        config = RetryConfig(max_attempts=3, base_delay_ms=1)
        with pytest.raises(ClientError):
            await retry_with_backoff(always_fail, config, {"ThrottlingException"})
        assert call_count == 3
