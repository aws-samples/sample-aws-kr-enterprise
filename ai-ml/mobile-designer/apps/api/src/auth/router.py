from typing import Annotated

from fastapi import APIRouter, Depends

from src.admin.config_service import SystemConfigService
from src.auth.dependencies import CurrentUser, get_jwt_service
from src.auth.jwt import JWTService
from src.auth.models import (
    ChangePasswordRequest,
    LoginRequest,
    PasswordResetConfirmModel,
    PasswordResetRequestModel,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserResponse,
)
from src.auth.service import AuthService
from src.common.config import Settings
from src.common.db.client import DynamoDBClient
from src.common.dependencies import get_db, get_settings_dep
from src.common.exceptions import ForbiddenException
from src.common.rate_limit import auth_rate_limit

router = APIRouter()


def get_auth_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> AuthService:
    return AuthService(db, jwt_service, settings)


def _get_config_service(
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> SystemConfigService:
    return SystemConfigService(db)


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(
    request: RegisterRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
    config_service: Annotated[SystemConfigService, Depends(_get_config_service)],
) -> UserResponse:
    config = await config_service.get_config()
    if not config.get("registrationOpen", True):
        raise ForbiddenException("Registration is currently closed")
    return await service.register(request)


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(auth_rate_limit)])
async def login(
    request: LoginRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    return await service.login(request.email, request.password)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: RefreshRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    return await service.refresh_token(request.refresh_token)


@router.post("/password-reset/request", status_code=202, dependencies=[Depends(auth_rate_limit)])
async def request_password_reset(
    request: PasswordResetRequestModel,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, str]:
    await service.request_password_reset(request.email)
    return {"message": "If the email exists, a reset link has been sent."}


@router.post("/password-reset/confirm", status_code=200)
async def confirm_password_reset(
    request: PasswordResetConfirmModel,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, str]:
    await service.confirm_password_reset(request.token, request.new_password)
    return {"message": "Password has been reset successfully."}


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: CurrentUser) -> UserResponse:
    return UserResponse(
        user_id=current_user["userId"],
        email=current_user["email"],
        name=current_user["name"],
        personal_team_id=current_user["personalTeamId"],
        created_at="",
        role=current_user.get("role", "user"),
    )


@router.patch("/password", status_code=200)
async def change_password(
    request: ChangePasswordRequest,
    current_user: CurrentUser,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, str]:
    await service.change_password(current_user["userId"], request.current_password, request.new_password)
    return {"message": "Password changed successfully."}


@router.patch("/profile", response_model=UserResponse)
async def update_profile(
    request: UpdateProfileRequest,
    current_user: CurrentUser,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> UserResponse:
    updated = await service.update_profile(current_user["userId"], request.name, request.email)
    return UserResponse(
        user_id=current_user["userId"],
        email=updated.get("email", current_user["email"]),
        name=updated.get("name", current_user["name"]),
        personal_team_id=current_user["personalTeamId"],
        created_at=updated.get("createdAt", ""),
        role=current_user.get("role", "user"),
    )
