from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field

from src.admin.config_service import SystemConfigService
from src.admin.service import AdminService
from src.auth.dependencies import CurrentAdmin
from src.common.db.client import DynamoDBClient
from src.common.dependencies import get_db, get_s3
from src.common.s3.client import S3Client
from src.prompts.models import CreatePromptRequest, PromptSlotSummary, PromptVersion
from src.prompts.service import PromptService

router = APIRouter()


class CreateUserRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=8, max_length=100)
    is_admin: bool = False


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=100)


class ChangeRoleRequest(BaseModel):
    role: str = Field(pattern="^(user|admin)$")


class UpdateConfigRequest(BaseModel):
    registrationOpen: bool | None = None
    maxUsers: int | None = None
    maintenanceMode: bool | None = None
    models: dict[str, str] | None = None


def get_admin_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> AdminService:
    return AdminService(db)


def get_config_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> SystemConfigService:
    return SystemConfigService(db)


def get_prompt_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
    s3: Annotated[S3Client, Depends(get_s3)],
) -> PromptService:
    return PromptService(db, s3)


@router.post("/users", status_code=201)
async def create_user(
    request: CreateUserRequest,
    _admin: CurrentAdmin,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> dict[str, Any]:
    return await service.create_user(request.email, request.name, request.password, request.is_admin)


@router.get("/users")
async def list_users(
    _admin: CurrentAdmin,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> list[dict[str, Any]]:
    return await service.list_users()


class ResetPasswordBody(BaseModel):
    new_password: str | None = None


@router.patch("/users/{user_id}/reset-password", status_code=200)
async def reset_user_password(
    user_id: str,
    admin: CurrentAdmin,
    service: Annotated[AdminService, Depends(get_admin_service)],
    request: ResetPasswordBody | None = None,
) -> dict[str, str]:
    if user_id == admin["userId"]:
        from src.common.exceptions import ValidationException
        raise ValidationException("자기 자신의 비밀번호는 리셋할 수 없습니다. 설정에서 변경하세요.")
    import secrets
    import string
    requested_password = request.new_password if request else None
    new_password = (requested_password if requested_password else
                    "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)))
    await service.reset_user_password(user_id, new_password)
    return {"message": "Password reset successfully.", "temp_password": new_password}


@router.patch("/users/{user_id}/role", status_code=200)
async def change_user_role(
    user_id: str,
    request: ChangeRoleRequest,
    _admin: CurrentAdmin,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> dict[str, str]:
    await service.change_user_role(user_id, request.role)
    return {"message": f"User role updated to '{request.role}'."}


@router.patch("/users/{user_id}/deactivate", status_code=200)
async def deactivate_user(
    user_id: str,
    _admin: CurrentAdmin,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> dict[str, str]:
    await service.deactivate_user(user_id)
    return {"message": "User deactivated."}


@router.delete("/users/{user_id}", status_code=200)
async def delete_user(
    user_id: str,
    _admin: CurrentAdmin,
    service: Annotated[AdminService, Depends(get_admin_service)],
) -> dict[str, str]:
    await service.delete_user(user_id)
    return {"message": "User deleted permanently."}


# ─── System Config ───


@router.get("/settings")
async def get_settings(
    _admin: CurrentAdmin,
    service: Annotated[SystemConfigService, Depends(get_config_service)],
) -> dict[str, Any]:
    return await service.get_config()


@router.patch("/settings")
async def update_settings(
    request: UpdateConfigRequest,
    _admin: CurrentAdmin,
    service: Annotated[SystemConfigService, Depends(get_config_service)],
) -> dict[str, Any]:
    updates = request.model_dump(exclude_none=True)
    return await service.update_config(updates)


# ─── Prompt Management ───


@router.get("/prompts", response_model=list[PromptSlotSummary])
async def list_prompt_slots(
    _admin: CurrentAdmin,
    service: Annotated[PromptService, Depends(get_prompt_service)],
) -> list[PromptSlotSummary]:
    return await service.list_slots()


@router.get("/prompts/{slot}", response_model=list[PromptVersion])
async def list_prompt_versions(
    slot: str,
    _admin: CurrentAdmin,
    service: Annotated[PromptService, Depends(get_prompt_service)],
) -> list[PromptVersion]:
    return await service.list_versions(slot)


@router.post("/prompts/{slot}", response_model=PromptVersion, status_code=201)
async def create_prompt_version(
    slot: str,
    request: CreatePromptRequest,
    admin: CurrentAdmin,
    service: Annotated[PromptService, Depends(get_prompt_service)],
) -> PromptVersion:
    return await service.create_version(
        slot=slot,
        title=request.title,
        content=request.content,
        user_id=admin["userId"],
    )


@router.patch("/prompts/{slot}/{version}/activate")
async def activate_prompt_version(
    slot: str,
    version: str,
    _admin: CurrentAdmin,
    service: Annotated[PromptService, Depends(get_prompt_service)],
) -> dict[str, str]:
    await service.activate_version(slot, version)
    return {"message": f"Version '{version}' activated for slot '{slot}'."}


@router.get("/prompts/{slot}/{version}")
async def get_prompt_content(
    slot: str,
    version: str,
    _admin: CurrentAdmin,
    service: Annotated[PromptService, Depends(get_prompt_service)],
) -> dict[str, str]:
    content = await service.get_content(slot, version)
    return {"slot": slot, "version": version, "content": content}
