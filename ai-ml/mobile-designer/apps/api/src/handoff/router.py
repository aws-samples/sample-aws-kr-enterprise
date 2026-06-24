from typing import Annotated

from fastapi import APIRouter, Depends

from src.ai.task_manager import task_manager
from src.auth.dependencies import CurrentUser
from src.common.db.client import DynamoDBClient
from src.common.dependencies import get_db, get_s3
from src.common.s3.client import S3Client
from src.handoff.models import (
    BuildVerifyRequest,
    BuildVerifyResponse,
    GenerateHandoffRequest,
    GenerateProjectRequest,
    GenerateProjectResponse,
    HandoffResponse,
    TaskStatusResponse,
)
from src.handoff.service import HandoffService
from src.projects.authorization import authorize_project_by_id

router = APIRouter()


def get_handoff_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
    s3: Annotated[S3Client, Depends(get_s3)],
) -> HandoffService:
    return HandoffService(db, s3)


@router.post("/generate", response_model=HandoffResponse)
async def generate_handoff(
    request: GenerateHandoffRequest,
    current_user: CurrentUser,
    service: Annotated[HandoffService, Depends(get_handoff_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> HandoffResponse:
    team_id = await authorize_project_by_id(db, request.project_id, current_user["userId"], "write")
    result = await service.generate_artifacts(
        request.project_id,
        request.version_id,
        team_id,
        current_user["userId"],
        request.handoff_type,
    )
    return HandoffResponse(**result)


@router.post("/generate-project", response_model=GenerateProjectResponse)
async def generate_project(
    request: GenerateProjectRequest,
    current_user: CurrentUser,
    service: Annotated[HandoffService, Depends(get_handoff_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> GenerateProjectResponse:
    """Start LLM-based Android project generation (async). Returns task_id for polling."""
    team_id = await authorize_project_by_id(db, request.project_id, current_user["userId"], "write")
    task_id = service.start_generate_project(request.project_id, request.version_id, team_id)
    return GenerateProjectResponse(task_id=task_id)


@router.get("/task/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    current_user: CurrentUser,
) -> TaskStatusResponse:
    """Poll task status for async handoff generation."""
    task = await task_manager.get_task_remote(task_id)
    if not task:
        return TaskStatusResponse(task_id=task_id, status="not_found", progress=0)
    return TaskStatusResponse(
        task_id=task.task_id,
        status=task.status,
        progress=task.progress,
        current_step=task.current_step,
        logs=[{"timestamp": log.timestamp, "step": log.step, "detail": log.detail} for log in task.logs],
        result=task.result,
        error=task.error,
    )


@router.get("/active/{project_id}", response_model=TaskStatusResponse)
async def get_active_handoff_task(
    project_id: str,
    current_user: CurrentUser,
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> TaskStatusResponse:
    """Get active or recently completed handoff task for a project."""
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    task = await task_manager.get_active_task_remote(project_id, "handoff")
    if not task:
        return TaskStatusResponse(task_id="", status="not_found", progress=0)
    return TaskStatusResponse(
        task_id=task.task_id,
        status=task.status,
        progress=task.progress,
        current_step=task.current_step,
        logs=[{"timestamp": log.timestamp, "step": log.step, "detail": log.detail} for log in task.logs],
        result=task.result,
        error=task.error,
    )


@router.get("/{project_id}/download-project")
async def download_project(
    project_id: str,
    current_user: CurrentUser,
    service: Annotated[HandoffService, Depends(get_handoff_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, str]:
    """Download the LLM-generated project ZIP (from latest.json metadata)."""
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    url = await service.get_llm_project_download_url(project_id)
    return {"download_url": url}


@router.get("/{project_id}/{version_id}/download")
async def download_handoff(
    project_id: str,
    version_id: str,
    current_user: CurrentUser,
    service: Annotated[HandoffService, Depends(get_handoff_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
    artifact_key: str | None = None,
) -> dict[str, str]:
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    url = await service.get_download_url(project_id, version_id, artifact_key)
    return {"download_url": url}


@router.post("/build-verify", response_model=BuildVerifyResponse)
async def build_verify(
    request: BuildVerifyRequest,
    current_user: CurrentUser,
    service: Annotated[HandoffService, Depends(get_handoff_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> BuildVerifyResponse:
    await authorize_project_by_id(db, request.project_id, current_user["userId"], "write")
    result = await service.build_verify(request.project_id, request.version_id)
    return BuildVerifyResponse(**result)
