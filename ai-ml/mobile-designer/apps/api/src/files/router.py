from typing import Annotated

from fastapi import APIRouter, Depends

from src.auth.dependencies import CurrentUser
from src.common.config import Settings
from src.common.db.client import DynamoDBClient
from src.common.dependencies import get_db, get_s3, get_settings_dep
from src.common.s3.client import S3Client
from src.files.models import FileResponse, PresignRequest, PresignResponse, UploadCompleteRequest
from src.files.service import FileService
from src.projects.authorization import authorize_project_by_id

router = APIRouter()


def get_file_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
    s3: Annotated[S3Client, Depends(get_s3)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> FileService:
    return FileService(db, s3, settings)


@router.post("/presign", response_model=PresignResponse, status_code=201)
async def request_presign(
    request: PresignRequest,
    current_user: CurrentUser,
    service: Annotated[FileService, Depends(get_file_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> PresignResponse:
    team_id = await authorize_project_by_id(db, request.project_id, current_user["userId"], "write")
    return await service.request_presign(request, current_user["userId"], team_id)


@router.post("/complete", status_code=200)
async def complete_upload(
    request: UploadCompleteRequest,
    current_user: CurrentUser,
    service: Annotated[FileService, Depends(get_file_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, str]:
    await authorize_project_by_id(db, request.project_id, current_user["userId"], "write")
    await service.complete_upload(request.project_id, request.file_id)
    return {"status": "completed"}


@router.delete("/{project_id}/{file_id}", status_code=204)
async def delete_file(
    project_id: str,
    file_id: str,
    current_user: CurrentUser,
    service: Annotated[FileService, Depends(get_file_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> None:
    await authorize_project_by_id(db, project_id, current_user["userId"], "write")
    await service.delete_file(project_id, file_id)


@router.get("/{project_id}", response_model=list[FileResponse])
async def list_files(
    project_id: str,
    current_user: CurrentUser,
    service: Annotated[FileService, Depends(get_file_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> list[FileResponse]:
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    items = await service.list_files(project_id)
    return [
        FileResponse(
            file_id=item["fileId"],
            project_id=item["projectId"],
            filename=item["filename"],
            content_type=item["contentType"],
            size=item["size"],
            file_type=item["fileType"],
            upload_status=item["uploadStatus"],
            created_at=item["createdAt"],
        )
        for item in items
    ]
