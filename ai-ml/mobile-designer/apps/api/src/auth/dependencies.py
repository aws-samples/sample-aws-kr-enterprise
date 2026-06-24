from typing import Annotated, Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.jwt import JWTService
from src.common.config import get_settings
from src.common.db.client import DynamoDBClient
from src.common.db.tables import USERS_TABLE
from src.common.dependencies import get_db
from src.common.exceptions import ForbiddenException, UnauthorizedException

_bearer_scheme = HTTPBearer()


def get_jwt_service() -> JWTService:
    return JWTService(get_settings())


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
    jwt_service: Annotated[JWTService, Depends(get_jwt_service)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, Any]:
    payload = jwt_service.verify_access_token(credentials.credentials)
    user_id = payload["sub"]

    user = await db.get_item(table_name=USERS_TABLE, key={"userId": user_id})
    if not user:
        raise UnauthorizedException("User not found")

    return {
        "userId": user["userId"],
        "email": user["email"],
        "name": user["name"],
        "personalTeamId": user["personalTeamId"],
        "role": user.get("role", "user"),
    }


async def get_current_admin(
    current_user: Annotated[dict[str, Any], Depends(get_current_user)],
) -> dict[str, Any]:
    if current_user.get("role") != "admin":
        raise ForbiddenException("Admin access required")
    return current_user


CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]
CurrentAdmin = Annotated[dict[str, Any], Depends(get_current_admin)]
