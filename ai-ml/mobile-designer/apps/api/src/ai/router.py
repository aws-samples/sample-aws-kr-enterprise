from typing import Annotated, Any

from fastapi import APIRouter, Depends

from src.ai.models import AgentInput, ChatHistoryResponse, ChatRequest, GenerateRequest, ModifyRequest
from src.ai.orchestrator import AIOrchestrator
from src.ai.task_manager import task_manager
from src.auth.dependencies import CurrentUser
from src.common.config import Settings
from src.common.db.client import DynamoDBClient
from src.common.dependencies import get_db, get_s3, get_settings_dep
from src.common.s3.client import S3Client
from src.projects.authorization import authorize_project_by_id

router = APIRouter()


def get_orchestrator(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
    s3: Annotated[S3Client, Depends(get_s3)],
) -> AIOrchestrator:
    return AIOrchestrator(settings, db, s3)


@router.get("/chat/history/{project_id}/{session_id}", response_model=ChatHistoryResponse)
async def get_chat_history(
    project_id: str,
    session_id: str,
    current_user: CurrentUser,
    orchestrator: Annotated[AIOrchestrator, Depends(get_orchestrator)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> ChatHistoryResponse:
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    return await orchestrator.get_chat_history(project_id, session_id)


@router.get("/chat/status/{project_id}/{session_id}")
async def get_chat_status(
    project_id: str,
    session_id: str,
    current_user: CurrentUser,
    orchestrator: Annotated[AIOrchestrator, Depends(get_orchestrator)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, bool]:
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    return {"is_responding": await orchestrator.is_chat_responding(project_id, session_id)}


@router.post("/chat")
async def chat(
    body: ChatRequest,
    current_user: CurrentUser,
    orchestrator: Annotated[AIOrchestrator, Depends(get_orchestrator)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, str]:
    await authorize_project_by_id(db, body.project_id, current_user["userId"], "write")
    task_id = orchestrator.start_chat(body)
    return {"task_id": task_id}


@router.post("/generate")
async def generate_design(
    body: GenerateRequest,
    current_user: CurrentUser,
    orchestrator: Annotated[AIOrchestrator, Depends(get_orchestrator)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, str]:
    team_id = await authorize_project_by_id(db, body.project_id, current_user["userId"], "write")
    agent_input = AgentInput(
        session_id=f"{body.project_id}-{body.stage}",
        project_id=body.project_id,
        command=body.command,
        stage=body.stage,
        context={"file_ids": body.file_ids},
    )
    task_id = orchestrator.start_generate(agent_input, current_user["userId"], team_id)
    return {"task_id": task_id}


@router.post("/modify")
async def modify_design(
    body: ModifyRequest,
    current_user: CurrentUser,
    orchestrator: Annotated[AIOrchestrator, Depends(get_orchestrator)],
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, str]:
    team_id = await authorize_project_by_id(db, body.project_id, current_user["userId"], "write")
    agent_input = AgentInput(
        session_id=f"{body.project_id}-{body.stage}",
        project_id=body.project_id,
        command=body.command,
        stage=body.stage,
        selected_component_id=body.selected_component_id,
    )
    task_id = orchestrator.start_modify(agent_input, current_user["userId"], team_id)
    return {"task_id": task_id}


@router.get("/tasks/{task_id}")
async def get_task_status(
    task_id: str,
    current_user: CurrentUser,
) -> dict[str, Any]:
    task = await task_manager.get_task_remote(task_id)
    if not task:
        return {"status": "not_found"}
    return task.model_dump()


@router.get("/tasks/active/{project_id}/{stage}")
async def get_active_task(
    project_id: str,
    stage: str,
    current_user: CurrentUser,
    db: Annotated[DynamoDBClient, Depends(get_db)],
) -> dict[str, Any]:
    await authorize_project_by_id(db, project_id, current_user["userId"], "read")
    task = await task_manager.get_active_task_remote(project_id, stage)
    if not task:
        return {"status": "none"}
    return task.model_dump()
