from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from ulid import ULID

from src.common.config import Settings
from src.common.exceptions import UnauthorizedException
from src.common.secrets import _cached_jwt_secret


class JWTService:
    def __init__(self, settings: Settings) -> None:
        self._secret = _cached_jwt_secret or settings.jwt_secret_name
        self._algorithm = settings.jwt_algorithm
        self.access_token_expire_minutes = settings.jwt_access_token_expire_minutes
        self._refresh_token_expire_days = settings.jwt_refresh_token_expire_days

    def create_access_token(self, user_id: str, email: str, role: str = "user") -> str:
        now = datetime.now(UTC)
        payload = {
            "sub": user_id,
            "email": email,
            "role": role,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=self.access_token_expire_minutes),
            "jti": str(ULID()),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def create_refresh_token(self, user_id: str) -> tuple[str, str]:
        now = datetime.now(UTC)
        token_id = str(ULID())
        payload = {
            "sub": user_id,
            "type": "refresh",
            "iat": now,
            "exp": now + timedelta(days=self._refresh_token_expire_days),
            "jti": token_id,
        }
        token = jwt.encode(payload, self._secret, algorithm=self._algorithm)
        return token, token_id

    def create_reset_token(self, user_id: str) -> str:
        now = datetime.now(UTC)
        payload = {
            "sub": user_id,
            "type": "reset",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(ULID()),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def verify_access_token(self, token: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except jwt.ExpiredSignatureError:
            raise UnauthorizedException("Token expired") from None
        except jwt.InvalidTokenError:
            raise UnauthorizedException("Invalid token") from None

        if payload.get("type") != "access":
            raise UnauthorizedException("Invalid token type")
        return payload

    def verify_refresh_token(self, token: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except jwt.ExpiredSignatureError:
            raise UnauthorizedException("Refresh token expired") from None
        except jwt.InvalidTokenError:
            raise UnauthorizedException("Invalid refresh token") from None

        if payload.get("type") != "refresh":
            raise UnauthorizedException("Invalid token type")
        return payload

    def verify_reset_token(self, token: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except jwt.ExpiredSignatureError:
            raise UnauthorizedException("Reset token expired") from None
        except jwt.InvalidTokenError:
            raise UnauthorizedException("Invalid reset token") from None

        if payload.get("type") != "reset":
            raise UnauthorizedException("Invalid token type")
        return payload

    def get_refresh_expiry(self) -> datetime:
        return datetime.now(UTC) + timedelta(days=self._refresh_token_expire_days)
