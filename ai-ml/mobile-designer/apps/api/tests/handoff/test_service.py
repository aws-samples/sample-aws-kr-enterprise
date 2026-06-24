import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.common.exceptions import NotFoundException, ValidationException
from src.handoff.models import ArtifactType
from src.handoff.service import HandoffService


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.get_item = AsyncMock(return_value=None)
    db.put_item = AsyncMock(return_value={})
    db.query = AsyncMock(return_value={"Items": []})
    db.update_item = AsyncMock(return_value={})
    return db


@pytest.fixture
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.put_object = AsyncMock(return_value=None)
    s3.get_object = AsyncMock(return_value=json.dumps({"screens": [{"name": "Home", "components": []}], "tokens": {}}).encode())
    s3.head_object = AsyncMock(return_value={"ContentLength": 5000})
    s3.generate_presigned_download_url = AsyncMock(return_value="https://download.url")
    return s3


@pytest.fixture
def service(mock_db: AsyncMock, mock_s3: AsyncMock) -> HandoffService:
    return HandoffService(mock_db, mock_s3)


class TestGenerateArtifacts:
    @pytest.mark.asyncio
    async def test_generates_zip_with_version_id(self, service: HandoffService, mock_db: AsyncMock, mock_s3: AsyncMock) -> None:
        mock_db.get_item.return_value = {
            "versionId": "v-1", "projectId": "p-1", "stageId": "design",
            "snapshotKey": "key/snap.json", "action": "initial", "command": "gen",
            "createdAt": "2026-01-01", "createdBy": "u-1",
        }
        result = await service.generate_artifacts("p-1", "v-1", "t-1", "u-1")
        assert result["project_id"] == "p-1"
        assert result["version_id"] == "v-1"
        # generate_artifacts now produces a single Compose project ZIP artifact.
        assert len(result["artifacts"]) == 1
        assert result["artifacts"][0]["type"] == ArtifactType.COMPOSE_PROJECT
        assert mock_s3.put_object.call_count >= 1

    @pytest.mark.asyncio
    async def test_no_version_and_no_designs_raises(self, service: HandoffService, mock_db: AsyncMock, mock_s3: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": []}
        mock_db.get_item.return_value = None
        with pytest.raises((ValidationException, NotFoundException)):
            await service.generate_artifacts("p-1", None, "t-1", "u-1")


class TestGetDownloadUrl:
    @pytest.mark.asyncio
    async def test_returns_presigned_url(self, service: HandoffService, mock_s3: AsyncMock) -> None:
        url = await service.get_download_url("p-1", "v-1")
        assert url == "https://download.url"

    @pytest.mark.asyncio
    async def test_missing_artifact_raises(self, service: HandoffService, mock_s3: AsyncMock) -> None:
        mock_s3.head_object.return_value = None
        with pytest.raises(NotFoundException):
            await service.get_download_url("p-1", "v-missing")


class TestBuildVerify:
    @pytest.mark.asyncio
    async def test_passed_when_artifact_exists(self, service: HandoffService, mock_s3: AsyncMock) -> None:
        result = await service.build_verify("p-1", "v-1")
        assert result["status"] == "passed"

    @pytest.mark.asyncio
    async def test_missing_artifact_raises(self, service: HandoffService, mock_s3: AsyncMock) -> None:
        mock_s3.head_object.return_value = None
        with pytest.raises(NotFoundException):
            await service.build_verify("p-1", "v-missing")
