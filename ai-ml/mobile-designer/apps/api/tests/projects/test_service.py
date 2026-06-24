import pytest
from unittest.mock import AsyncMock, MagicMock

from src.projects.models import CreateProjectRequest, StageStatus, StageType
from src.projects.service import ProjectService


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.put_item = AsyncMock(return_value={})
    db.get_item = AsyncMock(return_value=None)
    db.query = AsyncMock(return_value={"Items": []})
    db.update_item = AsyncMock(return_value={"Attributes": {}})
    return db


@pytest.fixture
def mock_s3() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def service(mock_db: AsyncMock, mock_s3: AsyncMock) -> ProjectService:
    return ProjectService(mock_db, mock_s3)


class TestProjectService:
    @pytest.mark.asyncio
    async def test_create_project(self, service: ProjectService, mock_db: AsyncMock) -> None:
        request = CreateProjectRequest(name="My App")
        result = await service.create_project(request, "user-1", "team-1")

        assert result.name == "My App"
        assert result.team_id == "team-1"
        assert result.current_stage == StageType.REQUIREMENTS
        assert result.created_by == "user-1"
        mock_db.put_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_project_not_found(self, service: ProjectService, mock_db: AsyncMock) -> None:
        from src.common.exceptions import NotFoundException

        mock_db.get_item.return_value = None
        with pytest.raises(NotFoundException):
            await service.get_project("team-1", "nonexistent")

    @pytest.mark.asyncio
    async def test_advance_stage_requires_completed(self, service: ProjectService, mock_db: AsyncMock) -> None:
        from src.common.exceptions import ValidationException

        mock_db.get_item.return_value = {
            "projectId": "p-1",
            "teamId": "t-1",
            "name": "Test",
            "currentStage": StageType.REQUIREMENTS,
            "stageStatus": {StageType.REQUIREMENTS: StageStatus.IN_PROGRESS},
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "createdBy": "user-1",
        }
        with pytest.raises(ValidationException):
            await service.advance_stage("t-1", "p-1")
