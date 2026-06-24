import hashlib
from datetime import UTC, datetime
from typing import Any, cast

import bcrypt
import structlog
from ulid import ULID

from src.auth.email import EmailService
from src.auth.jwt import JWTService
from src.auth.models import RegisterRequest, TokenResponse, UserResponse
from src.common.config import Settings
from src.common.db.client import DynamoDBClient
from src.common.db.tables import REFRESH_TOKENS_TABLE, TEAMS_TABLE, USERS_TABLE
from src.common.exceptions import ConflictException, NotFoundException, UnauthorizedException

logger = structlog.get_logger()


def _hash_token(token: str) -> str:
    """Store a SHA-256 digest of the refresh token, never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, db: DynamoDBClient, jwt_service: JWTService, settings: Settings) -> None:
        self._db = db
        self._jwt = jwt_service
        self._settings = settings
        self._email = EmailService(settings)

    async def _revoke_all_refresh_tokens(self, user_id: str) -> None:
        """Delete every stored refresh token for a user (logout-everywhere)."""
        result = await self._db.query(
            table_name=REFRESH_TOKENS_TABLE,
            key_condition_expression="userId = :uid",
            expression_values={":uid": user_id},
        )
        for item in result.get("Items", []):
            await self._db.delete_item(
                table_name=REFRESH_TOKENS_TABLE,
                key={"userId": user_id, "tokenId": item["tokenId"]},
            )

    async def register(self, request: RegisterRequest) -> UserResponse:
        email_lower = request.email.lower()

        existing = await self._db.query(
            table_name=USERS_TABLE,
            key_condition_expression="email = :email",
            expression_values={":email": email_lower},
            index_name="GSI-Email",
            limit=1,
        )
        if existing.get("Items"):
            raise ConflictException("Email already registered")

        user_id = str(ULID())
        team_id = str(ULID())
        now = datetime.now(UTC).isoformat()

        password_hash = bcrypt.hashpw(request.password.encode(), bcrypt.gensalt()).decode()

        user_item = {
            "userId": user_id,
            "email": email_lower,
            "name": request.name,
            "passwordHash": password_hash,
            "personalTeamId": team_id,
            "role": "user",
            "createdAt": now,
            "updatedAt": now,
        }
        await self._db.put_item(
            table_name=USERS_TABLE,
            item=user_item,
            ConditionExpression="attribute_not_exists(userId)",
        )

        team_meta = {
            "teamId": team_id,
            "sk": "TEAM#meta",
            "name": f"{request.name}'s Workspace",
            "type": "personal",
            "createdAt": now,
            "createdBy": user_id,
        }
        membership = {
            "teamId": team_id,
            "sk": f"MEMBER#{user_id}",
            "userId": user_id,
            "role": "owner",
            "joinedAt": now,
            "invitedBy": user_id,
        }
        await self._db.batch_write(TEAMS_TABLE, [team_meta, membership])

        logger.info("user_registered", user_id=user_id, email=email_lower)

        return UserResponse(
            user_id=user_id,
            email=email_lower,
            name=request.name,
            personal_team_id=team_id,
            created_at=now,
        )

    async def login(self, email: str, password: str) -> TokenResponse:
        email_lower = email.lower()

        result = await self._db.query(
            table_name=USERS_TABLE,
            key_condition_expression="email = :email",
            expression_values={":email": email_lower},
            index_name="GSI-Email",
            limit=1,
        )
        items = result.get("Items", [])
        if not items:
            raise UnauthorizedException("Invalid email or password")

        user = items[0]
        if not bcrypt.checkpw(password.encode(), user["passwordHash"].encode()):
            raise UnauthorizedException("Invalid email or password")

        user_role = user.get("role", "user")
        access_token = self._jwt.create_access_token(user["userId"], user["email"], role=user_role)
        refresh_token, token_id = self._jwt.create_refresh_token(user["userId"])

        now = datetime.now(UTC).isoformat()
        await self._db.put_item(
            table_name=REFRESH_TOKENS_TABLE,
            item={
                "userId": user["userId"],
                "tokenId": token_id,
                "tokenHash": _hash_token(refresh_token),
                "expiresAt": self._jwt.get_refresh_expiry().isoformat(),
                "createdAt": now,
            },
        )

        logger.info("user_login", user_id=user["userId"])

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._jwt.access_token_expire_minutes * 60,
            must_change_password=user.get("mustChangePassword", False),
        )

    async def refresh_token(self, refresh_token: str) -> TokenResponse:
        payload = self._jwt.verify_refresh_token(refresh_token)
        user_id = payload["sub"]
        token_id = payload["jti"]

        existing = await self._db.get_item(
            table_name=REFRESH_TOKENS_TABLE,
            key={"userId": user_id, "tokenId": token_id},
        )
        if not existing:
            raise UnauthorizedException("Invalid refresh token")

        # Reject a token whose stored digest does not match (e.g. a forged jti).
        if existing.get("tokenHash") != _hash_token(refresh_token):
            raise UnauthorizedException("Invalid refresh token")

        # Rotate: delete the used token so it cannot be replayed.
        await self._db.delete_item(
            table_name=REFRESH_TOKENS_TABLE,
            key={"userId": user_id, "tokenId": token_id},
        )

        user = await self._db.get_item(table_name=USERS_TABLE, key={"userId": user_id})
        if not user:
            raise UnauthorizedException("User not found")

        new_access = self._jwt.create_access_token(user_id, user["email"], role=user.get("role", "user"))
        new_refresh, new_token_id = self._jwt.create_refresh_token(user_id)

        now = datetime.now(UTC).isoformat()
        await self._db.put_item(
            table_name=REFRESH_TOKENS_TABLE,
            item={
                "userId": user_id,
                "tokenId": new_token_id,
                "tokenHash": _hash_token(new_refresh),
                "expiresAt": self._jwt.get_refresh_expiry().isoformat(),
                "createdAt": now,
            },
        )

        return TokenResponse(
            access_token=new_access,
            refresh_token=new_refresh,
            expires_in=self._jwt.access_token_expire_minutes * 60,
        )

    async def change_password(self, user_id: str, current_password: str, new_password: str) -> None:
        user = await self._db.get_item(table_name=USERS_TABLE, key={"userId": user_id})
        if not user:
            raise NotFoundException("User", user_id)

        if not bcrypt.checkpw(current_password.encode(), user["passwordHash"].encode()):
            raise UnauthorizedException("Current password is incorrect")

        password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        now = datetime.now(UTC).isoformat()

        await self._db.update_item(
            table_name=USERS_TABLE,
            key={"userId": user_id},
            update_expression="SET passwordHash = :ph, updatedAt = :now REMOVE mustChangePassword",
            expression_values={":ph": password_hash, ":now": now},
        )
        # Invalidate every existing session: a leaked refresh token must not survive
        # a password change.
        await self._revoke_all_refresh_tokens(user_id)
        logger.info("password_changed", user_id=user_id)

    async def update_profile(self, user_id: str, name: str, email: str | None = None) -> dict[str, Any]:
        user = await self._db.get_item(table_name=USERS_TABLE, key={"userId": user_id})
        if not user:
            raise NotFoundException("User", user_id)

        now = datetime.now(UTC).isoformat()
        update_expr = "SET #n = :name, updatedAt = :now"
        expr_values: dict[str, Any] = {":name": name, ":now": now}
        expr_names: dict[str, str] = {"#n": "name"}

        if email:
            email_lower = email.lower()
            if email_lower != user["email"]:
                existing = await self._db.query(
                    table_name=USERS_TABLE,
                    key_condition_expression="email = :email",
                    expression_values={":email": email_lower},
                    index_name="GSI-Email",
                    limit=1,
                )
                if existing.get("Items"):
                    raise ConflictException("Email already in use")
                update_expr += ", email = :email"
                expr_values[":email"] = email_lower

        result = await self._db.update_item(
            table_name=USERS_TABLE,
            key={"userId": user_id},
            update_expression=update_expr,
            expression_values=expr_values,
            expression_names=expr_names,
        )
        logger.info("profile_updated", user_id=user_id)
        return cast(dict[str, Any], result.get("Attributes", {}))

    async def request_password_reset(self, email: str) -> None:
        email_lower = email.lower()
        result = await self._db.query(
            table_name=USERS_TABLE,
            key_condition_expression="email = :email",
            expression_values={":email": email_lower},
            index_name="GSI-Email",
            limit=1,
        )
        if not result.get("Items"):
            return

        user = result["Items"][0]
        reset_token = self._jwt.create_reset_token(user["userId"])
        logger.info("password_reset_requested", user_id=user["userId"])
        # The reset token is delivered to the user by email only — never returned
        # from the API.
        reset_url = f"{self._settings.frontend_url}/reset-password"
        await self._email.send_password_reset_email(user["email"], reset_token, reset_url)

    async def confirm_password_reset(self, token: str, new_password: str) -> None:
        payload = self._jwt.verify_reset_token(token)
        user_id = payload["sub"]

        password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        now = datetime.now(UTC).isoformat()

        await self._db.update_item(
            table_name=USERS_TABLE,
            key={"userId": user_id},
            update_expression="SET passwordHash = :ph, updatedAt = :now",
            expression_values={":ph": password_hash, ":now": now},
        )
        await self._revoke_all_refresh_tokens(user_id)
        logger.info("password_reset_confirmed", user_id=user_id)
