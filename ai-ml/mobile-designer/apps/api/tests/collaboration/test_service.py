import pytest
from unittest.mock import AsyncMock
from datetime import UTC, datetime, timedelta

from src.collaboration.models import CreateCommentRequest
from src.collaboration.service import CollaborationService
from src.common.exceptions import ForbiddenException, NotFoundException


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.put_item = AsyncMock(return_value={})
    db.get_item = AsyncMock(return_value=None)
    db.query = AsyncMock(return_value={"Items": []})
    db.update_item = AsyncMock(return_value={})
    db.delete_item = AsyncMock(return_value={})
    return db


@pytest.fixture
def service(mock_db: AsyncMock) -> CollaborationService:
    return CollaborationService(mock_db)


class TestComments:
    @pytest.mark.asyncio
    async def test_create_comment(self, service: CollaborationService, mock_db: AsyncMock) -> None:
        req = CreateCommentRequest(
            project_id="p-1", screen_id="main", stage_id="wireframe", content="좋아 보여요",
        )
        result = await service.create_comment(req, "u-1")
        assert result.content == "좋아 보여요"
        assert result.resolved is False
        assert result.created_by == "u-1"
        mock_db.put_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_comment_with_component_id(self, service: CollaborationService) -> None:
        req = CreateCommentRequest(
            project_id="p-1", screen_id="main", stage_id="design",
            content="버튼 색상 변경", component_id="main-submit-btn",
        )
        result = await service.create_comment(req, "u-1")
        assert result.component_id == "main-submit-btn"

    @pytest.mark.asyncio
    async def test_list_comments_returns_results(self, service: CollaborationService, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": [
            {"commentId": "c-1", "projectId": "p-1", "screenId": "main", "stageId": "wireframe",
             "content": "hi", "resolved": False, "createdAt": "2026-01-01", "createdBy": "u-1"},
        ]}
        results = await service.list_comments("p-1", "main")
        assert len(results) == 1
        assert results[0].comment_id == "c-1"


class TestShareLinks:
    @pytest.mark.asyncio
    async def test_create_share_link_read_only(self, service: CollaborationService, mock_db: AsyncMock) -> None:
        result = await service.create_share_link("p-1", "t-1", "read_only", "u-1")
        assert len(result.share_token) == 64  # 32 bytes hex
        assert result.permission == "read_only"
        assert result.active is True

    @pytest.mark.asyncio
    async def test_create_share_link_with_expiry(self, service: CollaborationService) -> None:
        result = await service.create_share_link("p-1", "t-1", "edit", "u-1", expires_in_hours=24)
        assert result.expires_at is not None

    @pytest.mark.asyncio
    async def test_verify_active_link(self, service: CollaborationService, mock_db: AsyncMock) -> None:
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        mock_db.get_item.return_value = {
            "shareToken": "abc", "projectId": "p-1", "permission": "read_only",
            "active": True, "expiresAt": future,
        }
        result = await service.verify_share_link("abc")
        assert result["projectId"] == "p-1"

    @pytest.mark.asyncio
    async def test_verify_inactive_link_raises(self, service: CollaborationService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {"shareToken": "abc", "active": False}
        with pytest.raises(ForbiddenException, match="inactive"):
            await service.verify_share_link("abc")

    @pytest.mark.asyncio
    async def test_verify_expired_link_raises(self, service: CollaborationService, mock_db: AsyncMock) -> None:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        mock_db.get_item.return_value = {
            "shareToken": "abc", "active": True, "expiresAt": past,
        }
        with pytest.raises(ForbiddenException, match="expired"):
            await service.verify_share_link("abc")

    @pytest.mark.asyncio
    async def test_verify_nonexistent_link_raises(self, service: CollaborationService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = None
        with pytest.raises(NotFoundException):
            await service.verify_share_link("nonexistent")


class TestTeamMembers:
    @pytest.mark.asyncio
    async def test_add_member_existing_user(self, service: CollaborationService, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": [{"userId": "u-2", "email": "bob@test.com"}]}
        await service.add_team_member("t-1", "bob@test.com", "editor", "u-1")
        mock_db.put_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_member_nonexistent_user_raises(self, service: CollaborationService, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": []}
        with pytest.raises(NotFoundException):
            await service.add_team_member("t-1", "nobody@test.com", "editor", "u-1")
