import asyncio
import random
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import structlog
from botocore.exceptions import ClientError

logger = structlog.get_logger()

T = TypeVar("T")

DYNAMODB_RETRYABLE_ERRORS = {"ThrottlingException", "InternalServerError", "ServiceUnavailable"}
S3_RETRYABLE_STATUS_CODES = {500, 503}
S3_RETRYABLE_ERRORS = {"SlowDown", "RequestTimeout"}
BEDROCK_RETRYABLE_ERRORS = {"ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException"}
SES_RETRYABLE_ERRORS = {"ThrottlingException", "ServiceUnavailableException"}


class RetryConfig:
    def __init__(
        self,
        max_attempts: int = 3,
        base_delay_ms: float = 100.0,
        max_delay_ms: float = 5000.0,
        backoff_factor: float = 4.0,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay_ms = base_delay_ms
        self.max_delay_ms = max_delay_ms
        self.backoff_factor = backoff_factor


DYNAMODB_RETRY = RetryConfig(max_attempts=3, base_delay_ms=100, max_delay_ms=5000, backoff_factor=4)
S3_RETRY = RetryConfig(max_attempts=3, base_delay_ms=100, max_delay_ms=5000, backoff_factor=4)
BEDROCK_RETRY = RetryConfig(max_attempts=3, base_delay_ms=1000, max_delay_ms=30000, backoff_factor=4)
SES_RETRY = RetryConfig(max_attempts=2, base_delay_ms=5000, max_delay_ms=10000, backoff_factor=4)


def _is_retryable(error: Exception, retryable_errors: set[str]) -> bool:
    if isinstance(error, ClientError):
        error_code = error.response.get("Error", {}).get("Code", "")
        return error_code in retryable_errors
    return False


def _calculate_delay(attempt: int, config: RetryConfig) -> float:
    delay_ms = min(config.base_delay_ms * (config.backoff_factor**attempt), config.max_delay_ms)
    jittered_delay_ms = delay_ms * random.random()
    return jittered_delay_ms / 1000.0


async def retry_with_backoff(
    func: Callable[..., Coroutine[Any, Any, T]],
    config: RetryConfig,
    retryable_errors: set[str],
    *args: Any,
    **kwargs: Any,
) -> T:
    last_error: Exception | None = None

    for attempt in range(config.max_attempts):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if not _is_retryable(e, retryable_errors) or attempt == config.max_attempts - 1:
                raise

            delay = _calculate_delay(attempt, config)
            logger.warning(
                "retry_attempt",
                attempt=attempt + 1,
                max_attempts=config.max_attempts,
                delay_seconds=round(delay, 3),
                error=str(e),
            )
            await asyncio.sleep(delay)

    raise last_error  # type: ignore[misc]


async def retry_dynamodb(func: Callable[..., Coroutine[Any, Any, T]], *args: Any, **kwargs: Any) -> T:
    return await retry_with_backoff(func, DYNAMODB_RETRY, DYNAMODB_RETRYABLE_ERRORS, *args, **kwargs)


async def retry_s3(func: Callable[..., Coroutine[Any, Any, T]], *args: Any, **kwargs: Any) -> T:
    return await retry_with_backoff(func, S3_RETRY, S3_RETRYABLE_ERRORS, *args, **kwargs)


async def retry_bedrock(func: Callable[..., Coroutine[Any, Any, T]], *args: Any, **kwargs: Any) -> T:
    return await retry_with_backoff(func, BEDROCK_RETRY, BEDROCK_RETRYABLE_ERRORS, *args, **kwargs)


async def retry_ses(func: Callable[..., Coroutine[Any, Any, T]], *args: Any, **kwargs: Any) -> T:
    return await retry_with_backoff(func, SES_RETRY, SES_RETRYABLE_ERRORS, *args, **kwargs)
