from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SSEEventType(StrEnum):
    START = "start"
    PROGRESS = "progress"
    COMPONENT_UPDATE = "component_update"
    DESIGN_COMPLETE = "design_complete"
    ERROR = "error"
    PING = "ping"
    HEARTBEAT = "heartbeat"
    DONE = "done"


class SSEEvent(BaseModel):
    event: SSEEventType
    data: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class ChatRequest(BaseModel):
    project_id: str
    message: str
    session_id: str
    stage: str = "requirements"
    history: list[dict[str, str]] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    reply: str
    ready_to_proceed: bool = False


class ChatHistoryMessage(BaseModel):
    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    messages: list[ChatHistoryMessage]
    ready_to_proceed: bool = False


class GenerateRequest(BaseModel):
    project_id: str
    command: str
    stage: str
    file_ids: list[str] = Field(default_factory=list)


class ModifyRequest(BaseModel):
    project_id: str
    command: str
    stage: str
    selected_component_id: str | None = None


class AgentInput(BaseModel):
    session_id: str
    project_id: str
    command: str
    stage: str
    context: dict[str, Any] = Field(default_factory=dict)
    selected_component_id: str | None = None


class AgentOutput(BaseModel):
    design_data: dict[str, Any]
    components: list[dict[str, Any]]
    metadata: dict[str, Any] = Field(default_factory=dict)
