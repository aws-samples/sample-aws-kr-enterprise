from enum import StrEnum

from pydantic import BaseModel, Field


class FileType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    MARKDOWN = "md"
    TEXT = "txt"
    IMAGE = "image"


class UploadStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class PresignRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    content_type: str
    size: int = Field(gt=0)
    project_id: str


class PresignResponse(BaseModel):
    file_id: str
    upload_url: str
    key: str
    max_size_bytes: int


class UploadCompleteRequest(BaseModel):
    file_id: str
    project_id: str


class FileResponse(BaseModel):
    file_id: str
    project_id: str
    filename: str
    content_type: str
    size: int
    file_type: str
    upload_status: str
    created_at: str
