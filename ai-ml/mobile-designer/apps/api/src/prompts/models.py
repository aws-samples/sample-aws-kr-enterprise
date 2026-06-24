from pydantic import BaseModel


class PromptVersion(BaseModel):
    prompt_slot: str
    version: str
    is_active: bool
    title: str
    content_key: str
    created_by: str
    created_at: str


class PromptSlotSummary(BaseModel):
    slot: str
    active_version: str | None
    total_versions: int


class CreatePromptRequest(BaseModel):
    title: str
    content: str
