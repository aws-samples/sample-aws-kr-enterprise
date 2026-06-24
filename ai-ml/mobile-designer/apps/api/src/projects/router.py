from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from src.auth.dependencies import CurrentUser
from src.common.db.client import DynamoDBClient
from src.common.dependencies import get_db, get_s3
from src.common.s3.client import S3Client
from src.projects.authorization import authorize_project_access, authorize_project_by_id
from src.projects.models import (
    CreateProjectRequest,
    ProjectListResponse,
    ProjectResponse,
    RevertRequest,
    UpdateProjectRequest,
    VersionAction,
    VersionListResponse,
    VersionResponse,
)
from src.projects.service import ProjectService
from src.projects.version_service import VersionService

router = APIRouter()


def get_project_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
    s3: Annotated[S3Client, Depends(get_s3)],
) -> ProjectService:
    return ProjectService(db, s3)


def get_version_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
    s3: Annotated[S3Client, Depends(get_s3)],
) -> VersionService:
    return VersionService(db, s3)


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(
    request: CreateProjectRequest,
    current_user: CurrentUser,
    service: Annotated[ProjectService, Depends(get_project_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> ProjectResponse:
    team_id = request.team_id or current_user["personalTeamId"]
    await authorize_project_access(db, team_id, current_user["userId"], "write")
    return await service.create_project(request, current_user["userId"], team_id)


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    current_user: CurrentUser,
    service: Annotated[ProjectService, Depends(get_project_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
    team_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> ProjectListResponse:
    tid = team_id or current_user["personalTeamId"]
    await authorize_project_access(db, tid, current_user["userId"], "read")
    result = await service.list_projects(tid, limit, cursor)
    return ProjectListResponse(**result)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    current_user: CurrentUser,
    service: Annotated[ProjectService, Depends(get_project_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
    team_id: str | None = None,
) -> ProjectResponse:
    tid = team_id or current_user["personalTeamId"]
    await authorize_project_access(db, tid, current_user["userId"], "read")
    return await service.get_project(tid, project_id)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    request: UpdateProjectRequest,
    current_user: CurrentUser,
    service: Annotated[ProjectService, Depends(get_project_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
    team_id: str | None = None,
) -> ProjectResponse:
    tid = team_id or current_user["personalTeamId"]
    await authorize_project_access(db, tid, current_user["userId"], "write")
    return await service.update_project(tid, project_id, request.name)


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    current_user: CurrentUser,
    service: Annotated[ProjectService, Depends(get_project_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
    team_id: str | None = None,
) -> None:
    tid = team_id or current_user["personalTeamId"]
    await authorize_project_access(db, tid, current_user["userId"], "delete")
    await service.delete_project(tid, project_id)


@router.post("/{project_id}/advance-stage", response_model=ProjectResponse)
async def advance_stage(
    project_id: str,
    current_user: CurrentUser,
    service: Annotated[ProjectService, Depends(get_project_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
    team_id: str | None = None,
) -> ProjectResponse:
    tid = team_id or current_user["personalTeamId"]
    await authorize_project_access(db, tid, current_user["userId"], "write")
    return await service.advance_stage(tid, project_id)


@router.get("/{project_id}/versions", response_model=VersionListResponse)
async def list_versions(
    project_id: str,
    current_user: CurrentUser,
    version_service: Annotated[VersionService, Depends(get_version_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
    stage_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> VersionListResponse:
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    result = await version_service.list_versions(project_id, stage_id, limit, cursor)
    return VersionListResponse(**result)


@router.get("/{project_id}/stages/{stage_id}/snapshot")
async def get_stage_snapshot(
    project_id: str,
    stage_id: str,
    current_user: CurrentUser,
    version_service: Annotated[VersionService, Depends(get_version_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, Any]:
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    versions = await version_service.list_versions(project_id, stage_id, limit=1)
    if not versions["items"]:
        return {"design": None}
    snapshot_bytes = await version_service.get_snapshot(project_id, versions["items"][0].version_id)
    import json
    return {"design": json.loads(snapshot_bytes)}


@router.get("/{project_id}/versions/{version_id}", response_model=VersionResponse)
async def get_version(
    project_id: str,
    version_id: str,
    current_user: CurrentUser,
    version_service: Annotated[VersionService, Depends(get_version_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> VersionResponse:
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    return await version_service.get_version(project_id, version_id)


@router.post("/{project_id}/revert", response_model=VersionResponse)
async def revert_version(
    project_id: str,
    request: RevertRequest,
    current_user: CurrentUser,
    version_service: Annotated[VersionService, Depends(get_version_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> VersionResponse:
    tid = await authorize_project_by_id(db, project_id, current_user["userId"], "write")
    return await version_service.revert_to_version(
        project_id, tid, request.target_version_id, current_user["userId"]
    )


class UpdateTokensRequest(BaseModel):
    stage_id: str
    tokens: dict[str, Any]


@router.post("/{project_id}/tokens")
async def update_tokens(
    project_id: str,
    request: UpdateTokensRequest,
    current_user: CurrentUser,
    version_service: Annotated[VersionService, Depends(get_version_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, Any]:
    import json
    team_id = await authorize_project_by_id(db, project_id, current_user["userId"], "write")
    versions = await version_service.list_versions(project_id, request.stage_id, limit=1)
    if not versions["items"]:
        return {"error": "No design found for this stage"}

    current_version = versions["items"][0]
    snapshot_bytes = await version_service.get_snapshot(project_id, current_version.version_id)
    design = json.loads(snapshot_bytes)

    design["tokens"] = {**design.get("tokens", {}), **request.tokens}

    new_snapshot = json.dumps(design, ensure_ascii=False).encode()

    new_version = await version_service.create_version(
        project_id=project_id,
        team_id=team_id,
        stage_id=request.stage_id,
        action=VersionAction.TWEAK,
        command="토큰 직접 수정",
        snapshot_data=new_snapshot,
        user_id=current_user["userId"],
        parent_version_id=current_version.version_id,
    )

    return {"version_id": new_version.version_id, "status": "saved"}
