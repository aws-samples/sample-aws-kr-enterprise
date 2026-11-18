"""Validate client-owned Kiro authentication without exposing credentials."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from .timestamps import parse_timestamp

_EXPIRY = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)$")
_ARN = re.compile(r"^arn:[A-Za-z0-9-]+:codewhisperer:[a-z0-9-]+:\d{12}:profile/[A-Za-z0-9._-]+$")
_AUTH_METHODS = frozenset({"social", "IdC", "external_idp"})
_PROVIDERS = frozenset({"Google", "Github", "BuilderId", "Internal", "Enterprise", "ExternalIdp"})


def validate_client_token(value: Any, *, now: datetime | None = None) -> dict[str, str]:
    """Return only the supported Kiro response fields or raise a sanitized error."""
    if type(value) is not dict or set(value) - {"accessToken", "expiresAt", "profileArn", "authMethod", "provider"}:
        raise ValueError("Invalid client authentication response")
    token, expires = value.get("accessToken"), value.get("expiresAt")
    if (type(token) is not str or not 1 <= len(token) <= 16384 or
            any(ord(char) < 33 or ord(char) > 126 for char in token) or
            type(expires) is not str or len(expires) > 64 or _EXPIRY.fullmatch(expires) is None):
        raise ValueError("Invalid client authentication response")
    try:
        parsed = parse_timestamp(expires)
        current = now or datetime.now(timezone.utc)
        if parsed.tzinfo is None or parsed <= current + timedelta(seconds=185):
            raise ValueError("Invalid client authentication response")
    except (OverflowError, ValueError, TypeError):
        raise ValueError("Invalid client authentication response") from None
    result = {"accessToken": token, "expiresAt": expires}
    if "profileArn" in value:
        profile = value["profileArn"]
        if type(profile) is not str or len(profile) > 512 or _ARN.fullmatch(profile) is None:
            raise ValueError("Invalid client authentication response")
        result["profileArn"] = profile
    for field, allowed in (("authMethod", _AUTH_METHODS), ("provider", _PROVIDERS)):
        if field in value:
            item = value[field]
            if type(item) is not str or item not in allowed:
                raise ValueError("Invalid client authentication response")
            result[field] = item
    return result
