from datetime import UTC, datetime
from typing import Any, cast

import structlog
from ulid import ULID

from src.common.config import Settings
from src.common.db.client import DynamoDBClient
from src.common.db.tables import FILES_TABLE
from src.common.exceptions import NotFoundException, ValidationException
from src.common.s3.client import S3Client
from src.files.models import FileType, PresignRequest, PresignResponse, UploadStatus

logger = structlog.get_logger()

CONTENT_TYPE_TO_FILE_TYPE = {
    "application/pdf": FileType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCX,
    "text/markdown": FileType.MARKDOWN,
    "text/x-markdown": FileType.MARKDOWN,
    "text/plain": FileType.TEXT,
    "image/png": FileType.IMAGE,
    "image/jpeg": FileType.IMAGE,
    "image/webp": FileType.IMAGE,
}

EXTENSION_TO_FILE_TYPE = {
    ".pdf": FileType.PDF,
    ".docx": FileType.DOCX,
    ".md": FileType.MARKDOWN,
    ".markdown": FileType.MARKDOWN,
    ".txt": FileType.TEXT,
}


class FileService:
    def __init__(self, db: DynamoDBClient, s3: S3Client, settings: Settings) -> None:
        self._db = db
        self._s3 = s3
        self._settings = settings

    async def request_presign(self, request: PresignRequest, user_id: str, team_id: str) -> PresignResponse:
        max_bytes = self._settings.max_file_size_mb * 1024 * 1024
        if request.size > max_bytes:
            raise ValidationException(f"File size exceeds {self._settings.max_file_size_mb}MB limit")

        file_type = CONTENT_TYPE_TO_FILE_TYPE.get(request.content_type)
        if not file_type:
            # Fallback: detect by file extension
            import os
            ext = os.path.splitext(request.filename)[1].lower()
            file_type = EXTENSION_TO_FILE_TYPE.get(ext)
        if not file_type:
            raise ValidationException(f"Unsupported file type: {request.content_type} ({request.filename})")

        file_id = str(ULID())
        s3_key = f"projects/{request.project_id}/files/{file_id}/{request.filename}"
        now = datetime.now(UTC).isoformat()

        item = {
            "projectId": request.project_id,
            "sk": f"FILE#{file_id}",
            "fileId": file_id,
            "teamId": team_id,
            "filename": request.filename,
            "contentType": request.content_type,
            "size": request.size,
            "s3Key": s3_key,
            "fileType": file_type,
            "parsedContentKey": None,
            "uploadStatus": UploadStatus.PENDING,
            "createdAt": now,
            "uploadedBy": user_id,
        }
        await self._db.put_item(table_name=FILES_TABLE, item=item)

        presign_result = await self._s3.generate_presigned_upload_url(
            key=s3_key,
            content_type=request.content_type,
            max_size_bytes=max_bytes,
        )

        logger.info("presign_generated", file_id=file_id, project_id=request.project_id)

        return PresignResponse(
            file_id=file_id,
            upload_url=presign_result["url"],
            key=s3_key,
            max_size_bytes=max_bytes,
        )

    async def complete_upload(self, project_id: str, file_id: str) -> None:
        item = await self._db.get_item(
            table_name=FILES_TABLE,
            key={"projectId": project_id, "sk": f"FILE#{file_id}"},
        )
        if not item:
            raise NotFoundException("File", file_id)

        head = await self._s3.head_object(item["s3Key"])
        if not head:
            raise ValidationException("File not found in storage. Upload may have failed.")

        now = datetime.now(UTC).isoformat()
        await self._db.update_item(
            table_name=FILES_TABLE,
            key={"projectId": project_id, "sk": f"FILE#{file_id}"},
            update_expression="SET uploadStatus = :status, updatedAt = :now",
            expression_values={":status": UploadStatus.COMPLETED, ":now": now},
        )

        if item["fileType"] != FileType.IMAGE:
            await self._parse_file(project_id, file_id, item)

        logger.info("upload_completed", file_id=file_id, project_id=project_id)

    async def _parse_file(self, project_id: str, file_id: str, file_item: dict[str, Any]) -> None:
        from src.files.parsers import parse_file

        file_bytes = await self._s3.get_object(file_item["s3Key"])
        parsed_content = await parse_file(file_bytes, FileType(file_item["fileType"]), file_item["filename"])

        parsed_key = f"projects/{project_id}/files/{file_id}/parsed.txt"
        await self._s3.put_object(parsed_key, parsed_content.encode(), "text/plain")

        await self._db.update_item(
            table_name=FILES_TABLE,
            key={"projectId": project_id, "sk": f"FILE#{file_id}"},
            update_expression="SET parsedContentKey = :key",
            expression_values={":key": parsed_key},
        )

    async def delete_file(self, project_id: str, file_id: str) -> None:
        item = await self._db.get_item(
            table_name=FILES_TABLE,
            key={"projectId": project_id, "sk": f"FILE#{file_id}"},
        )
        if not item:
            raise NotFoundException("File", file_id)

        keys_to_delete = [item["s3Key"]]
        if item.get("parsedContentKey"):
            keys_to_delete.append(item["parsedContentKey"])
        await self._s3.delete_objects(keys_to_delete)

        await self._db.delete_item(
            table_name=FILES_TABLE,
            key={"projectId": project_id, "sk": f"FILE#{file_id}"},
        )
        logger.info("file_deleted", file_id=file_id, project_id=project_id)

    async def get_file(self, project_id: str, file_id: str) -> dict[str, Any]:
        item = await self._db.get_item(
            table_name=FILES_TABLE,
            key={"projectId": project_id, "sk": f"FILE#{file_id}"},
        )
        if not item:
            raise NotFoundException("File", file_id)
        return item

    async def list_files(self, project_id: str) -> list[dict[str, Any]]:
        result = await self._db.query(
            table_name=FILES_TABLE,
            key_condition_expression="projectId = :pid AND begins_with(sk, :prefix)",
            expression_values={":pid": project_id, ":prefix": "FILE#"},
        )
        return cast(list[dict[str, Any]], result.get("Items", []))
