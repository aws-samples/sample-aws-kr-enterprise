import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from ulid import ULID

from src.collaboration.models import CommentResponse, CreateCommentRequest, ShareLinkResponse
from src.common.db.client import DynamoDBClient
from src.common.db.tables import COMMENTS_TABLE, SHARE_LINKS_TABLE, TEAMS_TABLE, USERS_TABLE
from src.common.exceptions import ForbiddenException, NotFoundException

logger = structlog.get_logger()


class CollaborationService:
    def __init__(self, db: DynamoDBClient) -> None:
        self._db = db

    async def create_comment(self, request: CreateCommentRequest, user_id: str) -> CommentResponse:
        comment_id = str(ULID())
        now = datetime.now(UTC).isoformat()
        pk = f"{request.project_id}#{request.screen_id}"

        item = {
            "pk": pk,
            "commentId": comment_id,
            "projectId": request.project_id,
            "screenId": request.screen_id,
            "componentId": request.component_id,
            "stageId": request.stage_id,
            "content": request.content,
            "parentId": request.parent_id,
            "resolved": False,
            "createdAt": now,
            "createdBy": user_id,
        }
        await self._db.put_item(table_name=COMMENTS_TABLE, item=item)

        logger.info("comment_created", comment_id=comment_id, project_id=request.project_id)

        return CommentResponse(
            comment_id=comment_id,
            project_id=request.project_id,
            screen_id=request.screen_id,
            component_id=request.component_id,
            stage_id=request.stage_id,
            content=request.content,
            parent_id=request.parent_id,
            resolved=False,
            created_at=now,
            created_by=user_id,
        )

    async def list_comments(self, project_id: str, screen_id: str) -> list[CommentResponse]:
        pk = f"{project_id}#{screen_id}"
        result = await self._db.query(
            table_name=COMMENTS_TABLE,
            key_condition_expression="pk = :pk",
            expression_values={":pk": pk},
        )
        return [
            CommentResponse(
                comment_id=item["commentId"],
                project_id=item["projectId"],
                screen_id=item["screenId"],
                component_id=item.get("componentId"),
                stage_id=item["stageId"],
                content=item["content"],
                parent_id=item.get("parentId"),
                resolved=item.get("resolved", False),
                created_at=item["createdAt"],
                created_by=item["createdBy"],
            )
            for item in result.get("Items", [])
        ]

    async def resolve_comment(self, project_id: str, screen_id: str, comment_id: str, resolved: bool) -> None:
        pk = f"{project_id}#{screen_id}"
        now = datetime.now(UTC).isoformat()
        await self._db.update_item(
            table_name=COMMENTS_TABLE,
            key={"pk": pk, "commentId": comment_id},
            update_expression="SET resolved = :r, updatedAt = :now",
            expression_values={":r": resolved, ":now": now},
        )

    async def create_share_link(
        self, project_id: str, team_id: str, permission: str, user_id: str, expires_in_hours: int | None = None
    ) -> ShareLinkResponse:
        share_token = secrets.token_hex(32)
        now = datetime.now(UTC)
        expires_at = (now + timedelta(hours=expires_in_hours)).isoformat() if expires_in_hours else None

        item = {
            "shareToken": share_token,
            "projectId": project_id,
            "teamId": team_id,
            "permission": permission,
            "createdAt": now.isoformat(),
            "createdBy": user_id,
            "expiresAt": expires_at,
            "active": True,
        }
        await self._db.put_item(table_name=SHARE_LINKS_TABLE, item=item)

        logger.info("share_link_created", project_id=project_id, permission=permission)

        return ShareLinkResponse(
            share_token=share_token,
            project_id=project_id,
            permission=permission,
            expires_at=expires_at,
            active=True,
        )

    async def verify_share_link(self, share_token: str) -> dict[str, Any]:
        item = await self._db.get_item(
            table_name=SHARE_LINKS_TABLE,
            key={"shareToken": share_token},
        )
        if not item:
            raise NotFoundException("ShareLink", share_token)

        if not item.get("active", False):
            raise ForbiddenException("Share link is inactive")

        expires_at = item.get("expiresAt")
        if expires_at and datetime.fromisoformat(expires_at) < datetime.now(UTC):
            raise ForbiddenException("Share link has expired")

        return item

    async def deactivate_share_link(self, share_token: str) -> None:
        await self._db.update_item(
            table_name=SHARE_LINKS_TABLE,
            key={"shareToken": share_token},
            update_expression="SET active = :a",
            expression_values={":a": False},
        )

    async def add_team_member(self, team_id: str, user_email: str, role: str, inviter_id: str) -> None:
        result = await self._db.query(
            table_name=USERS_TABLE,
            key_condition_expression="email = :email",
            expression_values={":email": user_email.lower()},
            index_name="GSI-Email",
            limit=1,
        )
        if not result.get("Items"):
            raise NotFoundException("User", user_email)

        user = result["Items"][0]
        now = datetime.now(UTC).isoformat()

        membership = {
            "teamId": team_id,
            "sk": f"MEMBER#{user['userId']}",
            "userId": user["userId"],
            "role": role,
            "joinedAt": now,
            "invitedBy": inviter_id,
        }
        await self._db.put_item(table_name=TEAMS_TABLE, item=membership)
        logger.info("team_member_added", team_id=team_id, user_id=user["userId"], role=role)

    async def remove_team_member(self, team_id: str, user_id: str) -> None:
        await self._db.delete_item(
            table_name=TEAMS_TABLE,
            key={"teamId": team_id, "sk": f"MEMBER#{user_id}"},
        )
        logger.info("team_member_removed", team_id=team_id, user_id=user_id)

    async def list_team_members(self, team_id: str) -> list[dict[str, Any]]:
        result = await self._db.query(
            table_name=TEAMS_TABLE,
            key_condition_expression="teamId = :tid AND begins_with(sk, :prefix)",
            expression_values={":tid": team_id, ":prefix": "MEMBER#"},
        )
        return cast(list[dict[str, Any]], result.get("Items", []))
