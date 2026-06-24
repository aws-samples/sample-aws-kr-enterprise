from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class ArtifactType(StrEnum):
    COMPOSE_PROJECT = "compose_project"
    DESIGN_TOKENS = "design_tokens"
    FIGMA_TOKENS = "figma_tokens"
    COMPOSE_THEME = "compose_theme"
    DESIGN_SPEC = "design_spec"
    PREVIEW_PNG = "preview_png"
    README = "readme"


class HandoffType(StrEnum):
    FULL_PROJECT = "full_project"
    DESIGN_TOKENS = "design_tokens"
    FIGMA_TOKENS = "figma_tokens"
    COMPOSE_THEME = "compose_theme"
    DESIGN_SPEC = "design_spec"


class GenerateHandoffRequest(BaseModel):
    project_id: str
    version_id: str | None = None
    handoff_type: HandoffType = HandoffType.FULL_PROJECT


class HandoffResponse(BaseModel):
    project_id: str
    version_id: str
    artifacts: list[dict[str, Any]]
    download_url: str | None = None
    build_status: str | None = None


class GenerateProjectRequest(BaseModel):
    project_id: str
    version_id: str | None = None


class GenerateProjectResponse(BaseModel):
    task_id: str


class TaskLogEntry(BaseModel):
    timestamp: str
    step: str
    detail: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    progress: int = 0
    current_step: str | None = None
    logs: list[TaskLogEntry] = []
    result: dict[str, Any] | None = None
    error: str | None = None


class BuildVerifyRequest(BaseModel):
    project_id: str
    version_id: str


class BuildVerifyResponse(BaseModel):
    status: str
    message: str
    errors: list[str] | None = None
