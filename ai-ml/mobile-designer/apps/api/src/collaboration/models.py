from pydantic import BaseModel, Field


class CreateCommentRequest(BaseModel):
    project_id: str
    screen_id: str
    stage_id: str
    content: str = Field(min_length=1, max_length=2000)
    component_id: str | None = None
    parent_id: str | None = None


class CommentResponse(BaseModel):
    comment_id: str
    project_id: str
    screen_id: str
    component_id: str | None
    stage_id: str
    content: str
    parent_id: str | None
    resolved: bool
    created_at: str
    created_by: str


class ResolveCommentRequest(BaseModel):
    resolved: bool = True


class CreateShareLinkRequest(BaseModel):
    project_id: str
    team_id: str
    permission: str = "read_only"
    expires_in_hours: int | None = None


class ShareLinkResponse(BaseModel):
    share_token: str
    project_id: str
    permission: str
    expires_at: str | None
    active: bool


class TeamMemberRequest(BaseModel):
    email: str
    role: str = "editor"
