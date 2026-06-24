import pytest
from unittest.mock import AsyncMock

from src.common.exceptions import NotFoundException
from src.projects.models import VersionAction
from src.projects.version_service import VersionService


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.put_item = AsyncMock(return_value={})
    db.get_item = AsyncMock(return_value=None)
    db.query = AsyncMock(return_value={"Items": []})
    db.update_item = AsyncMock(return_value={})
    return db


@pytest.fixture
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.put_object = AsyncMock(return_value=None)
    s3.get_object = AsyncMock(return_value=b'{"screens": []}')
    return s3


@pytest.fixture
def service(mock_db: AsyncMock, mock_s3: AsyncMock) -> VersionService:
    return VersionService(mock_db, mock_s3)


class TestCreateVersion:
    @pytest.mark.asyncio
    async def test_creates_version_and_uploads_snapshot(self, service: VersionService, mock_db: AsyncMock, mock_s3: AsyncMock) -> None:
        result = await service.create_version(
            project_id="p-1", team_id="t-1", stage_id="wireframe",
            action=VersionAction.INITIAL, command="Generate wireframe",
            snapshot_data=b'{"test": true}', user_id="u-1",
        )
        assert result.project_id == "p-1"
        assert result.stage_id == "wireframe"
        assert result.action == "initial"
        assert result.command == "Generate wireframe"
        mock_s3.put_object.assert_called_once()
        mock_db.put_item.assert_called_once()
        mock_db.update_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_version_id_is_unique(self, service: VersionService) -> None:
        r1 = await service.create_version("p-1", "t-1", "wireframe", VersionAction.INITIAL, "cmd", b"{}", "u-1")
        r2 = await service.create_version("p-1", "t-1", "wireframe", VersionAction.MODIFY, "cmd2", b"{}", "u-1")
        assert r1.version_id != r2.version_id

    @pytest.mark.asyncio
    async def test_parent_version_id_stored(self, service: VersionService, mock_db: AsyncMock) -> None:
        result = await service.create_version(
            "p-1", "t-1", "wireframe", VersionAction.MODIFY, "modify",
            b"{}", "u-1", parent_version_id="v-parent",
        )
        assert result.parent_version_id == "v-parent"


class TestGetVersion:
    @pytest.mark.asyncio
    async def test_get_existing_version(self, service: VersionService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = {
            "versionId": "v-1", "projectId": "p-1", "stageId": "wireframe",
            "action": "initial", "command": "gen", "createdAt": "2026-01-01", "createdBy": "u-1",
        }
        result = await service.get_version("p-1", "v-1")
        assert result.version_id == "v-1"

    @pytest.mark.asyncio
    async def test_get_nonexistent_version_raises(self, service: VersionService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = None
        with pytest.raises(NotFoundException):
            await service.get_version("p-1", "v-none")


class TestRevertToVersion:
    @pytest.mark.asyncio
    async def test_revert_creates_new_version_with_old_snapshot(self, service: VersionService, mock_db: AsyncMock, mock_s3: AsyncMock) -> None:
        mock_db.get_item.return_value = {
            "versionId": "v-old", "projectId": "p-1", "stageId": "wireframe",
            "action": "initial", "command": "original", "snapshotKey": "key/snap.json",
            "createdAt": "2026-01-01", "createdBy": "u-1",
        }
        mock_s3.get_object.return_value = b'{"restored": true}'
        mock_db.query.return_value = {"Items": [{"versionId": "v-latest"}]}

        result = await service.revert_to_version("p-1", "t-1", "v-old", "u-2")
        assert result.action == "revert"
        assert result.command == "Revert to v-old"
        assert result.parent_version_id == "v-latest"

    @pytest.mark.asyncio
    async def test_revert_nonexistent_version_raises(self, service: VersionService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = None
        with pytest.raises(NotFoundException):
            await service.revert_to_version("p-1", "t-1", "v-gone", "u-1")
