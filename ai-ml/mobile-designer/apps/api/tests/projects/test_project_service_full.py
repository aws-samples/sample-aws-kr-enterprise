import pytest
from unittest.mock import AsyncMock

from src.common.exceptions import NotFoundException, ValidationException
from src.projects.models import StageStatus, StageType
from src.projects.service import ProjectService, STAGE_ORDER


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.put_item = AsyncMock(return_value={})
    db.get_item = AsyncMock(return_value=None)
    db.query = AsyncMock(return_value={"Items": [], "LastEvaluatedKey": None})
    db.update_item = AsyncMock(return_value={"Attributes": {}})
    db.delete_item = AsyncMock(return_value={})
    return db


@pytest.fixture
def mock_s3() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def service(mock_db: AsyncMock, mock_s3: AsyncMock) -> ProjectService:
    return ProjectService(mock_db, mock_s3)


class TestListProjects:
    @pytest.mark.asyncio
    async def test_empty_list(self, service: ProjectService, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": []}
        result = await service.list_projects("t-1")
        assert result["items"] == []
        assert result["next_cursor"] is None

    @pytest.mark.asyncio
    async def test_with_items(self, service: ProjectService, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": [
            {"projectId": "p-1", "teamId": "t-1", "name": "App", "currentStage": "requirements",
             "stageStatus": {}, "createdAt": "2026-01-01", "updatedAt": "2026-01-01", "createdBy": "u-1"},
        ]}
        result = await service.list_projects("t-1")
        assert len(result["items"]) == 1

    @pytest.mark.asyncio
    async def test_pagination_cursor(self, service: ProjectService, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": [], "LastEvaluatedKey": {"teamId": "t-1", "sk": "PROJECT#p-5"}}
        result = await service.list_projects("t-1", limit=5)
        assert result["next_cursor"] == "PROJECT#p-5"


class TestUpdateProject:
    @pytest.mark.asyncio
    async def test_updates_name(self, service: ProjectService, mock_db: AsyncMock) -> None:
        mock_db.update_item.return_value = {"Attributes": {
            "projectId": "p-1", "teamId": "t-1", "name": "New Name",
            "currentStage": "requirements", "stageStatus": {},
            "createdAt": "2026-01-01", "updatedAt": "2026-01-02", "createdBy": "u-1",
        }}
        result = await service.update_project("t-1", "p-1", "New Name")
        assert result.name == "New Name"


class TestDeleteProject:
    @pytest.mark.asyncio
    async def test_calls_delete(self, service: ProjectService, mock_db: AsyncMock) -> None:
        await service.delete_project("t-1", "p-1")
        mock_db.delete_item.assert_called_once()


class TestAdvanceStage:
    @pytest.mark.asyncio
    async def test_advances_from_requirements_to_wireframe(self, service: ProjectService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {
            "projectId": "p-1", "teamId": "t-1", "name": "App",
            "currentStage": StageType.REQUIREMENTS,
            "stageStatus": {StageType.REQUIREMENTS: StageStatus.COMPLETED, StageType.WIREFRAME: StageStatus.NOT_STARTED},
            "createdAt": "2026-01-01", "updatedAt": "2026-01-01", "createdBy": "u-1",
        }
        mock_db.update_item.return_value = {"Attributes": {
            "projectId": "p-1", "teamId": "t-1", "name": "App",
            "currentStage": StageType.WIREFRAME, "stageStatus": {StageType.WIREFRAME: StageStatus.IN_PROGRESS},
            "createdAt": "2026-01-01", "updatedAt": "2026-01-02", "createdBy": "u-1",
        }}
        result = await service.advance_stage("t-1", "p-1")
        assert result.current_stage == StageType.WIREFRAME

    @pytest.mark.asyncio
    async def test_cannot_advance_past_handoff(self, service: ProjectService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {
            "projectId": "p-1", "teamId": "t-1", "name": "App",
            "currentStage": StageType.HANDOFF,
            "stageStatus": {StageType.HANDOFF: StageStatus.COMPLETED},
            "createdAt": "2026-01-01", "updatedAt": "2026-01-01", "createdBy": "u-1",
        }
        with pytest.raises(ValidationException, match="final stage"):
            await service.advance_stage("t-1", "p-1")

    @pytest.mark.asyncio
    async def test_cannot_advance_if_not_completed(self, service: ProjectService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {
            "projectId": "p-1", "teamId": "t-1", "name": "App",
            "currentStage": StageType.REQUIREMENTS,
            "stageStatus": {StageType.REQUIREMENTS: StageStatus.IN_PROGRESS},
            "createdAt": "2026-01-01", "updatedAt": "2026-01-01", "createdBy": "u-1",
        }
        with pytest.raises(ValidationException, match="not completed"):
            await service.advance_stage("t-1", "p-1")

    @pytest.mark.asyncio
    async def test_project_not_found_raises(self, service: ProjectService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = None
        with pytest.raises(NotFoundException):
            await service.advance_stage("t-1", "p-nonexistent")


class TestStageOrder:
    def test_stage_order_sequence(self) -> None:
        assert STAGE_ORDER == [StageType.REQUIREMENTS, StageType.WIREFRAME, StageType.DESIGN, StageType.HANDOFF]
