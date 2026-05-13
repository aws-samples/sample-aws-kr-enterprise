"""Valkey connection factory — singleton per Lambda cold start.

Prefers valkey-glide (AWS-official client). Falls back to redis-py if glide
is unavailable.
"""

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)

_client: Any = None
_secret_cache: str | None = None


def _get_secret(secret_arn: str) -> str:
    """Retrieve and cache the Valkey AUTH token from Secrets Manager."""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret_string: str = response["SecretString"]

    result: str
    try:
        parsed = json.loads(secret_string)
        val = parsed.get("password")
        result = val if isinstance(val, str) else secret_string
    except (json.JSONDecodeError, TypeError):
        result = secret_string

    _secret_cache = result
    return result


def get_valkey_client():
    """Return a singleton Valkey client (created once per cold start).

    Tries valkey-glide first, falls back to redis-py.
    TLS is enabled; connection and socket timeouts are set for Lambda use.
    """
    global _client
    if _client is not None:
        return _client

    endpoint = os.environ["VALKEY_ENDPOINT"]
    secret_arn = os.environ["VALKEY_SECRET_ARN"]
    password = _get_secret(secret_arn)

    # Try valkey-glide first (AWS-official client)
    try:
        from glide import GlideClient, GlideClientConfiguration, NodeAddress

        config = GlideClientConfiguration(
            addresses=[NodeAddress(host=endpoint, port=6379)],
            use_tls=True,
            credentials={"password": password},
            request_timeout=5000,  # 5s connection timeout
        )

        import asyncio

        _client = asyncio.run(GlideClient.create(config))
        return _client

    except ImportError:
        pass
    except Exception as e:
        logger.warning("valkey-glide init failed, falling back to redis-py: %s", e)

    # Fallback to redis-py (wire-compatible with Valkey)
    try:
        import redis

        _client = redis.Redis(
            host=endpoint,
            port=6379,
            password=password,
            ssl=True,
            socket_connect_timeout=5,
            socket_timeout=2,
            decode_responses=True,
            retry_on_timeout=True,
        )
        # Verify connection
        _client.ping()
        return _client

    except ImportError as e:
        raise RuntimeError(
            "Neither valkey-glide nor redis-py is available. "
            "Install at least one: pip install valkey-glide or pip install redis"
        ) from e


def reset_client():
    """Reset the cached client (useful for testing or reconnection)."""
    global _client, _secret_cache
    _client = None
    _secret_cache = None
