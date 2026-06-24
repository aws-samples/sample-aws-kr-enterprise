import pytest
from unittest.mock import AsyncMock

from src.common.config import Settings
from src.common.exceptions import NotFoundException, ValidationException
from src.files.models import PresignRequest
from src.files.service import FileService


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, max_file_size_mb=20, s3_bucket_name="test-bucket")


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.put_item = AsyncMock(return_value={})
    db.get_item = AsyncMock(return_value=None)
    db.update_item = AsyncMock(return_value={})
    db.query = AsyncMock(return_value={"Items": []})
    return db


@pytest.fixture
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.generate_presigned_upload_url = AsyncMock(return_value={"url": "https://s3.example.com/upload", "key": "k", "fields": {}})
    s3.head_object = AsyncMock(return_value={"ContentLength": 1024})
    s3.get_object = AsyncMock(return_value=b"Hello world content")
    s3.put_object = AsyncMock(return_value=None)
    return s3


@pytest.fixture
def service(mock_db: AsyncMock, mock_s3: AsyncMock, settings: Settings) -> FileService:
    return FileService(mock_db, mock_s3, settings)


class TestRequestPresign:
    @pytest.mark.asyncio
    async def test_valid_pdf_presign(self, service: FileService) -> None:
        req = PresignRequest(filename="doc.pdf", content_type="application/pdf", size=1024, project_id="p-1")
        result = await service.request_presign(req, "u-1", "t-1")
        assert result.file_id != ""
        assert result.upload_url == "https://s3.example.com/upload"
        assert result.max_size_bytes == 20 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_file_too_large_raises(self, service: FileService) -> None:
        req = PresignRequest(filename="big.pdf", content_type="application/pdf", size=50 * 1024 * 1024, project_id="p-1")
        with pytest.raises(ValidationException, match="exceeds"):
            await service.request_presign(req, "u-1", "t-1")

    @pytest.mark.asyncio
    async def test_unsupported_content_type_raises(self, service: FileService) -> None:
        req = PresignRequest(filename="script.exe", content_type="application/x-executable", size=100, project_id="p-1")
        with pytest.raises(ValidationException, match="Unsupported"):
            await service.request_presign(req, "u-1", "t-1")

    @pytest.mark.asyncio
    async def test_image_type_accepted(self, service: FileService) -> None:
        req = PresignRequest(filename="screen.png", content_type="image/png", size=2048, project_id="p-1")
        result = await service.request_presign(req, "u-1", "t-1")
        assert result.file_id != ""


class TestCompleteUpload:
    @pytest.mark.asyncio
    async def test_complete_marks_as_completed(self, service: FileService, mock_db: AsyncMock, mock_s3: AsyncMock) -> None:
        mock_db.get_item.return_value = {
            "projectId": "p-1", "sk": "FILE#f-1", "fileId": "f-1",
            "s3Key": "projects/p-1/files/f-1/doc.pdf", "fileType": "pdf", "filename": "doc.pdf",
        }
        await service.complete_upload("p-1", "f-1")
        mock_db.update_item.assert_called()

    @pytest.mark.asyncio
    async def test_complete_nonexistent_file_raises(self, service: FileService, mock_db: AsyncMock) -> None:
        mock_db.get_item.return_value = None
        with pytest.raises(NotFoundException):
            await service.complete_upload("p-1", "f-nonexistent")

    @pytest.mark.asyncio
    async def test_complete_file_not_in_s3_raises(self, service: FileService, mock_db: AsyncMock, mock_s3: AsyncMock) -> None:
        mock_db.get_item.return_value = {
            "projectId": "p-1", "sk": "FILE#f-1", "fileId": "f-1",
            "s3Key": "projects/p-1/files/f-1/doc.pdf", "fileType": "pdf", "filename": "doc.pdf",
        }
        mock_s3.head_object.return_value = None
        with pytest.raises(ValidationException, match="not found in storage"):
            await service.complete_upload("p-1", "f-1")

    @pytest.mark.asyncio
    async def test_image_upload_does_not_trigger_parse(self, service: FileService, mock_db: AsyncMock, mock_s3: AsyncMock) -> None:
        mock_db.get_item.return_value = {
            "projectId": "p-1", "sk": "FILE#f-1", "fileId": "f-1",
            "s3Key": "projects/p-1/files/f-1/img.png", "fileType": "image", "filename": "img.png",
        }
        await service.complete_upload("p-1", "f-1")
        # parse would call s3.get_object for file content — should not happen for images
        mock_s3.get_object.assert_not_called()
