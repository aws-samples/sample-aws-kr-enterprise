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
    # Matches the payload the Playground UI actually posts: {approved, comment}.
    # `action` is optional/advisory metadata (the platform does not enforce an
    # approval gate — feedback is recorded but never blocks execution).
    approved: bool
    comment: str = ""
    action: str = ""
    resolvedBy: str = ""


class BuilderChatRequest(BaseModel):
    messages: list[dict]
    sessionId: str = ""
    state: str = "INIT"
