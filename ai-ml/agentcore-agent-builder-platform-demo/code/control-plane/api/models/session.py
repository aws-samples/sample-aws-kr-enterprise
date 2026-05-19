"""Pydantic models for Session, Event, HITL. Spec Section 5.1."""

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    sessionId: str = ""


class SessionMeta(BaseModel):
    sessionId: str
    agentId: str
    userId: str = ""
    trigger: str = "chat"
    startedAt: str = ""
    status: str = "in_progress"


class SessionEvent(BaseModel):
    type: str
    data: dict = {}
    agentId: str = ""


class HITLRequest(BaseModel):
    action: str
    approved: bool
    resolvedBy: str = ""


class BuilderChatRequest(BaseModel):
    messages: list[dict]
    sessionId: str = ""
    state: str = "INIT"
