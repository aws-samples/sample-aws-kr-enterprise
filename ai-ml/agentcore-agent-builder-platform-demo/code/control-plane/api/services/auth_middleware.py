"""Application-level Cognito JWT authentication middleware.

Verifies JWT tokens from Cognito User Pool on every API request.
Excludes: /health, /api/auth/*, /docs, /openapi.json
"""

import os
import json
import time
import logging
from typing import Optional

import httpx
from jose import jwt, JWTError, jwk
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

REGION = os.environ.get("AWS_REGION", "us-west-2")
USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")

EXCLUDED_PATHS = [
    "/health",
    "/api/auth/",
    "/docs",
    "/openapi.json",
    "/redoc",
]

_jwks_cache: dict = {}
_jwks_cache_time: float = 0
JWKS_CACHE_TTL = 3600


async def _get_jwks() -> dict:
    """Fetch and cache JWKS from Cognito."""
    global _jwks_cache, _jwks_cache_time

    if _jwks_cache and (time.time() - _jwks_cache_time) < JWKS_CACHE_TTL:
        return _jwks_cache

    jwks_url = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/jwks.json"
    async with httpx.AsyncClient() as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_cache_time = time.time()
        return _jwks_cache


def _decode_token(token: str, jwks: dict) -> Optional[dict]:
    """Decode and verify a Cognito JWT token."""
    try:
        headers = jwt.get_unverified_headers(token)
        kid = headers.get("kid")

        key = None
        for k in jwks.get("keys", []):
            if k["kid"] == kid:
                key = k
                break

        if not key:
            return None

        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}",
            options={"verify_exp": True},
        )
        return claims
    except JWTError as e:
        logger.debug(f"JWT verification failed: {e}")
        return None


class CognitoAuthMiddleware(BaseHTTPMiddleware):
    """Verify Cognito JWT on all API requests except excluded paths."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if any(path.startswith(exc) for exc in EXCLUDED_PATHS):
            return await call_next(request)

        if request.method == "OPTIONS":
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[7:]

        if not USER_POOL_ID:
            logger.warning("COGNITO_USER_POOL_ID not set, skipping auth")
            return await call_next(request)

        try:
            jwks = await _get_jwks()
        except Exception as e:
            logger.error(f"Failed to fetch JWKS: {e}")
            return JSONResponse(
                status_code=503,
                content={"detail": "Authentication service unavailable"},
            )

        claims = _decode_token(token, jwks)
        if not claims:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired token"},
            )

        request.state.user = claims
        request.state.user_email = claims.get("email", "")
        return await call_next(request)
