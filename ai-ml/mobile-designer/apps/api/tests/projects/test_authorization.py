import pytest
from unittest.mock import AsyncMock

from src.common.exceptions import ForbiddenException
from src.projects.authorization import authorize_project_access, get_user_role_in_team


@pytest.fixture
def mock_db() -> AsyncMock:
    return AsyncMock()


class TestGetUserRoleInTeam:
    @pytest.mark.asyncio
    async def test_returns_role_when_member(self, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {"role": "editor", "joinedAt": "2026-01-01"}
        role = await get_user_role_in_team(mock_db, "t-1", "u-1")
        assert role == "editor"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_member(self, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = None
        role = await get_user_role_in_team(mock_db, "t-1", "u-999")
        assert role is None


class TestAuthorizeProjectAccess:
    @pytest.mark.asyncio
    async def test_owner_has_all_permissions(self, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {"role": "owner"}
        role = await authorize_project_access(mock_db, "t-1", "u-1", "delete")
        assert role == "owner"

    @pytest.mark.asyncio
    async def test_editor_can_write(self, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {"role": "editor"}
        role = await authorize_project_access(mock_db, "t-1", "u-1", "write")
        assert role == "editor"

    @pytest.mark.asyncio
    async def test_editor_cannot_delete(self, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {"role": "editor"}
        with pytest.raises(ForbiddenException):
            await authorize_project_access(mock_db, "t-1", "u-1", "delete")

    @pytest.mark.asyncio
    async def test_viewer_can_only_read(self, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {"role": "viewer"}
        role = await authorize_project_access(mock_db, "t-1", "u-1", "read")
        assert role == "viewer"

    @pytest.mark.asyncio
    async def test_viewer_cannot_write(self, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {"role": "viewer"}
        with pytest.raises(ForbiddenException):
            await authorize_project_access(mock_db, "t-1", "u-1", "write")

    @pytest.mark.asyncio
    async def test_non_member_raises_forbidden(self, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = None
        with pytest.raises(ForbiddenException, match="not a member"):
            await authorize_project_access(mock_db, "t-1", "u-1", "read")
