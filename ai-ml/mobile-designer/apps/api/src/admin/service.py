from datetime import UTC, datetime
from typing import Any

import bcrypt
import structlog
from ulid import ULID

from src.common.db.client import DynamoDBClient
from src.common.db.tables import TEAMS_TABLE, USERS_TABLE
from src.common.exceptions import ConflictException, NotFoundException

logger = structlog.get_logger()


class AdminService:
    def __init__(self, db: DynamoDBClient) -> None:
        self._db = db

    async def create_user(self, email: str, name: str, password: str, is_admin: bool = False) -> dict[str, Any]:
        email_lower = email.lower()

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

        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        user_item = {
            "userId": user_id,
            "email": email_lower,
            "name": name,
            "passwordHash": password_hash,
            "personalTeamId": team_id,
            "role": "admin" if is_admin else "user",
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
            "name": f"{name}'s Workspace",
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

        logger.info("admin_created_user", user_id=user_id, email=email_lower, role=user_item["role"])

        return {
            "userId": user_id,
            "email": email_lower,
            "name": name,
            "role": user_item["role"],
            "personalTeamId": team_id,
            "createdAt": now,
        }

    async def list_users(self) -> list[dict[str, Any]]:
        result = await self._db.scan(table_name=USERS_TABLE)
        items = result.get("Items", [])
        users = []
        for item in items:
            users.append({
                "userId": item["userId"],
                "email": item["email"],
                "name": item["name"],
                "role": item.get("role", "user"),
                "personalTeamId": item.get("personalTeamId", ""),
                "createdAt": item.get("createdAt", ""),
                "status": item.get("status", "active"),
            })
        return users

    async def reset_user_password(self, user_id: str, new_password: str) -> None:
        user = await self._db.get_item(table_name=USERS_TABLE, key={"userId": user_id})
        if not user:
            raise NotFoundException("User", user_id)

        password_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        now = datetime.now(UTC).isoformat()

        await self._db.update_item(
            table_name=USERS_TABLE,
            key={"userId": user_id},
            update_expression="SET passwordHash = :ph, mustChangePassword = :mcp, updatedAt = :now",
            expression_values={":ph": password_hash, ":mcp": True, ":now": now},
        )
        logger.info("admin_reset_password", user_id=user_id)

    async def change_user_role(self, user_id: str, role: str) -> None:
        user = await self._db.get_item(table_name=USERS_TABLE, key={"userId": user_id})
        if not user:
            raise NotFoundException("User", user_id)

        now = datetime.now(UTC).isoformat()

        await self._db.update_item(
            table_name=USERS_TABLE,
            key={"userId": user_id},
            update_expression="SET #r = :role, updatedAt = :now",
            expression_values={":role": role, ":now": now},
            expression_names={"#r": "role"},
        )
        logger.info("admin_changed_role", user_id=user_id, new_role=role)

    async def deactivate_user(self, user_id: str) -> None:
        user = await self._db.get_item(table_name=USERS_TABLE, key={"userId": user_id})
        if not user:
            raise NotFoundException("User", user_id)

        now = datetime.now(UTC).isoformat()

        await self._db.update_item(
            table_name=USERS_TABLE,
            key={"userId": user_id},
            update_expression="SET #s = :status, updatedAt = :now",
            expression_values={":status": "inactive", ":now": now},
            expression_names={"#s": "status"},
        )
        logger.info("admin_deactivated_user", user_id=user_id)

    async def delete_user(self, user_id: str) -> None:
        user = await self._db.get_item(table_name=USERS_TABLE, key={"userId": user_id})
        if not user:
            raise NotFoundException("User", user_id)

        await self._db.delete_item(table_name=USERS_TABLE, key={"userId": user_id})
        logger.info("admin_deleted_user", user_id=user_id)
