from enum import StrEnum

from pydantic import BaseModel, Field


class StageType(StrEnum):
    REQUIREMENTS = "requirements"
    WIREFRAME = "wireframe"
    DESIGN = "design"
    HANDOFF = "handoff"


class StageStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PROPAGATION_FAILED = "propagation_failed"


class VersionAction(StrEnum):
    INITIAL = "initial"
    MODIFY = "modify"
    REVERT = "revert"
    PROPAGATE = "propagate"
    TWEAK = "tweak"


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    team_id: str | None = None


class UpdateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectResponse(BaseModel):
    project_id: str
    team_id: str
    name: str
    current_stage: StageType
    stage_status: dict[str, str]
    created_at: str
    updated_at: str
    created_by: str


class ProjectListResponse(BaseModel):
    items: list[ProjectResponse]
    next_cursor: str | None = None


class VersionResponse(BaseModel):
    version_id: str
    project_id: str
    stage_id: str
    action: str
    command: str
    parent_version_id: str | None
    created_at: str
    created_by: str


class VersionListResponse(BaseModel):
    items: list[VersionResponse]
    next_cursor: str | None = None


class RevertRequest(BaseModel):
    target_version_id: str
