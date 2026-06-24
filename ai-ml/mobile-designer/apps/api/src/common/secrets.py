"""Resolve secrets from environment or AWS Secrets Manager."""

import json

import aioboto3
import structlog

from src.common.config import Settings

logger = structlog.get_logger()

_cached_jwt_secret: str | None = None


async def resolve_jwt_secret(settings: Settings) -> str:
    global _cached_jwt_secret

    if _cached_jwt_secret:
        return _cached_jwt_secret

    if settings.jwt_secret_source == "secretsmanager":
        session = aioboto3.Session()
        async with session.client("secretsmanager", region_name=settings.aws_region) as client:
            response = await client.get_secret_value(SecretId=settings.jwt_secret_name)
            secret_string = response["SecretString"]
            try:
                secret_data = json.loads(secret_string)
                _cached_jwt_secret = secret_data.get("signing_key", secret_string)
            except json.JSONDecodeError:
                _cached_jwt_secret = secret_string
            logger.info("jwt_secret_loaded_from_secretsmanager", secret_name=settings.jwt_secret_name)
    else:
        _cached_jwt_secret = settings.jwt_secret_name

    return _cached_jwt_secret
