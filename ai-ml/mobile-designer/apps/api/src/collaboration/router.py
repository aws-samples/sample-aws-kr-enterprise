from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from src.auth.dependencies import CurrentUser
from src.collaboration.models import (
    CommentResponse,
    CreateCommentRequest,
    CreateShareLinkRequest,
    ResolveCommentRequest,
    ShareLinkResponse,
    TeamMemberRequest,
)
from src.collaboration.service import CollaborationService
from src.common.db.client import DynamoDBClient
from src.common.dependencies import get_db
from src.projects.authorization import authorize_project_access, authorize_project_by_id

router = APIRouter()


def get_collaboration_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> CollaborationService:
    return CollaborationService(db)


@router.post("/comments", response_model=CommentResponse, status_code=201)
async def create_comment(
    request: CreateCommentRequest,
    current_user: CurrentUser,
    service: Annotated[CollaborationService, Depends(get_collaboration_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> CommentResponse:
    await authorize_project_by_id(db, request.project_id, current_user["userId"], "write")
    return await service.create_comment(request, current_user["userId"])


@router.get("/comments", response_model=list[CommentResponse])
async def list_comments(
    project_id: str,
    screen_id: str,
    current_user: CurrentUser,
    service: Annotated[CollaborationService, Depends(get_collaboration_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> list[CommentResponse]:
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    return await service.list_comments(project_id, screen_id)


@router.patch("/comments/{comment_id}/resolve")
async def resolve_comment(
    comment_id: str,
    request: ResolveCommentRequest,
    current_user: CurrentUser,
    service: Annotated[CollaborationService, Depends(get_collaboration_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
    project_id: str = Query(...),
    screen_id: str = Query(...),
) -> dict[str, str]:
    await authorize_project_by_id(db, project_id, current_user["userId"], "write")
    await service.resolve_comment(project_id, screen_id, comment_id, request.resolved)
    return {"status": "updated"}


@router.post("/share", response_model=ShareLinkResponse, status_code=201)
async def create_share_link(
    request: CreateShareLinkRequest,
    current_user: CurrentUser,
    service: Annotated[CollaborationService, Depends(get_collaboration_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> ShareLinkResponse:
    await authorize_project_access(db, request.team_id, current_user["userId"], "manage_project")
    return await service.create_share_link(
        request.project_id, request.team_id, request.permission,
        current_user["userId"], request.expires_in_hours,
    )


@router.get("/share/{share_token}")
async def verify_share_link(
    share_token: str,
    service: Annotated[CollaborationService, Depends(get_collaboration_service)],
) -> dict[str, Any]:
    item = await service.verify_share_link(share_token)
    return {"project_id": item["projectId"], "permission": item["permission"]}


@router.delete("/share/{share_token}", status_code=204)
async def deactivate_share_link(
    share_token: str,
    current_user: CurrentUser,
    service: Annotated[CollaborationService, Depends(get_collaboration_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> None:
    link = await service.verify_share_link(share_token)
    await authorize_project_access(db, link["teamId"], current_user["userId"], "manage_project")
    await service.deactivate_share_link(share_token)


@router.post("/teams/{team_id}/members", status_code=201)
async def add_team_member(
    team_id: str,
    request: TeamMemberRequest,
    current_user: CurrentUser,
    service: Annotated[CollaborationService, Depends(get_collaboration_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, str]:
    await authorize_project_access(db, team_id, current_user["userId"], "manage_team")
    await service.add_team_member(team_id, request.email, request.role, current_user["userId"])
    return {"status": "invited"}


@router.delete("/teams/{team_id}/members/{user_id}", status_code=204)
async def remove_team_member(
    team_id: str,
    user_id: str,
    current_user: CurrentUser,
    service: Annotated[CollaborationService, Depends(get_collaboration_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> None:
    await authorize_project_access(db, team_id, current_user["userId"], "manage_team")
    await service.remove_team_member(team_id, user_id)


@router.get("/teams/{team_id}/members")
async def list_team_members(
    team_id: str,
    current_user: CurrentUser,
    service: Annotated[CollaborationService, Depends(get_collaboration_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> list[dict[str, Any]]:
    await authorize_project_access(db, team_id, current_user["userId"], "read")
    return await service.list_team_members(team_id)
