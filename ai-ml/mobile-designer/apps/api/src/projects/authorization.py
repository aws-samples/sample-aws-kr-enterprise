from src.common.db.client import DynamoDBClient
from src.common.db.tables import PROJECTS_TABLE, TEAMS_TABLE
from src.common.exceptions import ForbiddenException, NotFoundException

ROLE_PERMISSIONS = {
    "owner": {"read", "write", "delete", "manage_team", "manage_project"},
    "editor": {"read", "write"},
    "viewer": {"read"},
}


async def get_user_role_in_team(db: DynamoDBClient, team_id: str, user_id: str) -> str | None:
    item = await db.get_item(
        table_name=TEAMS_TABLE,
        key={"teamId": team_id, "sk": f"MEMBER#{user_id}"},
    )
    if not item:
        return None
    return item.get("role")


def _require_permission(role: str, required_permission: str) -> None:
    permissions = ROLE_PERMISSIONS.get(role, set())
    if required_permission not in permissions:
        raise ForbiddenException(f"Role '{role}' does not have '{required_permission}' permission")


async def authorize_team_access(
    db: DynamoDBClient, team_id: str, user_id: str, required_permission: str
) -> str:
    """Verify the user is a member of the team and holds the required permission.

    Returns the user's role in the team. Raises ForbiddenException otherwise.
    """
    role = await get_user_role_in_team(db, team_id, user_id)
    if role is None:
        raise ForbiddenException("You are not a member of this team")

    _require_permission(role, required_permission)
    return role


# Backwards-compatible alias used by the projects router where the team is known.
authorize_project_access = authorize_team_access


async def authorize_project_by_id(
    db: DynamoDBClient, project_id: str, user_id: str, required_permission: str
) -> str:
    """Resolve the owning team of a project by scanning the user's team memberships.

    Used by project-scoped routers (files, versions, handoff, ai) that only carry a
    project_id. Returns the owning team_id. Raises ForbiddenException if the user has
    no team containing this project, or NotFoundException if no such project exists in
    any of the user's teams.
    """
    memberships = await db.query(
        table_name=TEAMS_TABLE,
        key_condition_expression="userId = :uid",
        expression_values={":uid": user_id},
        index_name="GSI-UserTeams",
    )
    for membership in memberships.get("Items", []):
        team_id: str = membership["teamId"]
        role = membership.get("role")
        if role is None:
            continue
        project = await db.get_item(
            table_name=PROJECTS_TABLE,
            key={"teamId": team_id, "sk": f"PROJECT#{project_id}"},
        )
        if project:
            _require_permission(role, required_permission)
            return team_id

    raise NotFoundException("Project", project_id)
