"""Background task manager for AI generation / handoff jobs.

State is kept in-process for fast, synchronous access (the Strands progress
callback is synchronous and cannot await), and additionally written through to
DynamoDB so that progress polling survives ALB load-balancing across multiple API
instances and task restarts. Rows carry a TTL so the table self-cleans.

When no DynamoDB client is configured (unit tests), the manager degrades to a
pure in-memory store with identical behavior.
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, Field
from ulid import ULID

if TYPE_CHECKING:
    from src.common.db.client import DynamoDBClient

logger = structlog.get_logger()

# Tasks are ephemeral; expire DynamoDB rows a day after creation.
_TASK_TTL_SECONDS = 24 * 60 * 60


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskLog(BaseModel):
    timestamp: str
    step: str
    detail: str


class GenerateTask(BaseModel):
    task_id: str
    project_id: str
    stage: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    current_step: str = ""
    logs: list[TaskLog] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = ""
    completed_at: str | None = None


class ChatTaskStatus(BaseModel):
    is_responding: bool = False
    project_id: str = ""
    session_id: str = ""


def _project_stage_key(project_id: str, stage: str) -> str:
    return f"{project_id}:{stage}"


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, GenerateTask] = {}
        self._project_tasks: dict[str, str] = {}
        self._chat_status: dict[str, ChatTaskStatus] = {}
        self._db: DynamoDBClient | None = None

    # ─── DynamoDB wiring (set at app startup; absent in unit tests) ───

    def set_db(self, db: "DynamoDBClient") -> None:
        self._db = db

    def _table(self) -> str:
        from src.common.db.tables import AI_TASKS_TABLE

        return AI_TASKS_TABLE

    def _to_item(self, task: GenerateTask) -> dict[str, Any]:
        created = task.created_at or datetime.now(UTC).isoformat()
        return {
            "taskId": task.task_id,
            "projectStage": _project_stage_key(task.project_id, task.stage),
            "createdAt": created,
            "kind": "task",
            "data": task.model_dump(mode="json"),
            "ttl": int(datetime.now(UTC).timestamp()) + _TASK_TTL_SECONDS,
        }

    def _persist_task(self, task: GenerateTask) -> None:
        """Fire-and-forget write-through to DynamoDB (no-op without a loop/db)."""
        if self._db is None:
            return
        item = self._to_item(task)
        db = self._db
        table = self._table()

        async def _write() -> None:
            try:
                await db.put_item(table_name=table, item=item)
            except Exception as e:  # never let persistence break the job
                logger.warning("task_persist_failed", task_id=item["taskId"], error=str(e))

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop (sync test context) — local state only
        loop.create_task(_write())

    # ─── Chat responding flag ───

    def set_chat_responding(self, project_id: str, session_id: str, responding: bool) -> None:
        key = _project_stage_key(project_id, session_id)
        if responding:
            self._chat_status[key] = ChatTaskStatus(is_responding=True, project_id=project_id, session_id=session_id)
        else:
            self._chat_status.pop(key, None)
        self._persist_chat(project_id, session_id, responding)

    def _persist_chat(self, project_id: str, session_id: str, responding: bool) -> None:
        if self._db is None:
            return
        db = self._db
        table = self._table()
        task_id = f"CHAT#{project_id}:{session_id}"
        item = {
            "taskId": task_id,
            "projectStage": _project_stage_key(project_id, session_id),
            "createdAt": datetime.now(UTC).isoformat(),
            "kind": "chat",
            "isResponding": responding,
            "ttl": int(datetime.now(UTC).timestamp()) + _TASK_TTL_SECONDS,
        }

        async def _write() -> None:
            try:
                await db.put_item(table_name=table, item=item)
            except Exception as e:
                logger.warning("chat_status_persist_failed", task_id=task_id, error=str(e))

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_write())

    def is_chat_responding(self, project_id: str, session_id: str) -> bool:
        key = _project_stage_key(project_id, session_id)
        status = self._chat_status.get(key)
        return status.is_responding if status else False

    async def is_chat_responding_remote(self, project_id: str, session_id: str) -> bool:
        """Local-first, then DynamoDB fallback for cross-instance polling."""
        local = self._chat_status.get(_project_stage_key(project_id, session_id))
        if local is not None:
            return local.is_responding
        if self._db is None:
            return False
        with contextlib.suppress(Exception):
            item = await self._db.get_item(
                table_name=self._table(),
                key={"taskId": f"CHAT#{project_id}:{session_id}"},
            )
            if item:
                return bool(item.get("isResponding", False))
        return False

    # ─── Task lifecycle (synchronous; safe inside sync callbacks) ───

    def create_task(self, project_id: str, stage: str) -> GenerateTask:
        task_id = str(ULID())
        task = GenerateTask(
            task_id=task_id,
            project_id=project_id,
            stage=stage,
            status=TaskStatus.PENDING,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._tasks[task_id] = task
        self._project_tasks[_project_stage_key(project_id, stage)] = task_id
        self._persist_task(task)
        return task

    def get_task(self, task_id: str) -> GenerateTask | None:
        return self._tasks.get(task_id)

    def get_active_task(self, project_id: str, stage: str) -> GenerateTask | None:
        task_id = self._project_tasks.get(_project_stage_key(project_id, stage))
        if not task_id:
            return None
        task = self._tasks.get(task_id)
        if task and task.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED):
            return task
        return None

    def update_task(
        self,
        task_id: str,
        status: TaskStatus | None = None,
        progress: int | None = None,
        current_step: str | None = None,
        log_step: str | None = None,
        log_detail: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return

        if status:
            task.status = status
        if progress is not None:
            task.progress = progress
        if current_step:
            task.current_step = current_step
        if log_step and log_detail:
            task.logs.append(TaskLog(
                timestamp=datetime.now(UTC).isoformat(),
                step=log_step,
                detail=log_detail,
            ))
        if result is not None:
            task.result = result
        if error:
            task.error = error
        if status == TaskStatus.COMPLETED or status == TaskStatus.FAILED:
            task.completed_at = datetime.now(UTC).isoformat()

        self._persist_task(task)

    # ─── Remote-aware reads for HTTP polling endpoints ───

    async def get_task_remote(self, task_id: str) -> GenerateTask | None:
        local = self._tasks.get(task_id)
        if local is not None:
            return local
        if self._db is None:
            return None
        with contextlib.suppress(Exception):
            item = await self._db.get_item(table_name=self._table(), key={"taskId": task_id})
            if item and item.get("kind") == "task":
                return GenerateTask.model_validate(item["data"])
        return None

    async def get_active_task_remote(self, project_id: str, stage: str) -> GenerateTask | None:
        local = self.get_active_task(project_id, stage)
        if local is not None:
            return local
        if self._db is None:
            return None
        with contextlib.suppress(Exception):
            result = await self._db.query(
                table_name=self._table(),
                key_condition_expression="projectStage = :ps",
                expression_values={":ps": _project_stage_key(project_id, stage)},
                index_name="GSI-ProjectStage",
                scan_forward=False,
                limit=1,
            )
            items = result.get("Items", [])
            if items and items[0].get("kind") == "task":
                task = GenerateTask.model_validate(items[0]["data"])
                if task.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED):
                    return task
        return None


task_manager = TaskManager()
