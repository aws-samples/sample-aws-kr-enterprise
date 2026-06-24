from datetime import UTC, datetime
from typing import Any

import structlog
from ulid import ULID

from src.common.db.client import DynamoDBClient
from src.common.db.tables import PROJECTS_TABLE
from src.common.exceptions import NotFoundException, ValidationException
from src.common.s3.client import S3Client
from src.projects.models import (
    CreateProjectRequest,
    ProjectResponse,
    StageStatus,
    StageType,
)

logger = structlog.get_logger()

STAGE_ORDER = [StageType.REQUIREMENTS, StageType.WIREFRAME, StageType.DESIGN, StageType.HANDOFF]


class ProjectService:
    def __init__(self, db: DynamoDBClient, s3: S3Client) -> None:
        self._db = db
        self._s3 = s3

    async def create_project(self, request: CreateProjectRequest, user_id: str, team_id: str) -> ProjectResponse:
        project_id = str(ULID())
        now = datetime.now(UTC).isoformat()

        stage_status = {
            StageType.REQUIREMENTS: StageStatus.NOT_STARTED,
            StageType.WIREFRAME: StageStatus.NOT_STARTED,
            StageType.DESIGN: StageStatus.NOT_STARTED,
            StageType.HANDOFF: StageStatus.NOT_STARTED,
        }

        item = {
            "teamId": team_id,
            "sk": f"PROJECT#{project_id}",
            "projectId": project_id,
            "name": request.name,
            "currentStage": StageType.REQUIREMENTS,
            "stageStatus": {k: v for k, v in stage_status.items()},
            "latestVersionIds": {},
            "createdAt": now,
            "createdBy": user_id,
            "updatedAt": now,
        }

        await self._db.put_item(table_name=PROJECTS_TABLE, item=item)
        logger.info("project_created", project_id=project_id, team_id=team_id)

        return self._to_response(item)

    async def get_project(self, team_id: str, project_id: str) -> ProjectResponse:
        item = await self._db.get_item(
            table_name=PROJECTS_TABLE,
            key={"teamId": team_id, "sk": f"PROJECT#{project_id}"},
        )
        if not item:
            raise NotFoundException("Project", project_id)
        return self._to_response(item)

    async def list_projects(self, team_id: str, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "table_name": PROJECTS_TABLE,
            "key_condition_expression": "teamId = :tid AND begins_with(sk, :prefix)",
            "expression_values": {":tid": team_id, ":prefix": "PROJECT#"},
            "scan_forward": False,
            "limit": limit,
        }
        if cursor:
            kwargs["exclusive_start_key"] = {"teamId": team_id, "sk": cursor}

        result = await self._db.query(**kwargs)
        items = [self._to_response(i) for i in result.get("Items", [])]
        last_key = result.get("LastEvaluatedKey")

        return {
            "items": items,
            "next_cursor": last_key["sk"] if last_key else None,
        }

    async def update_project(self, team_id: str, project_id: str, name: str) -> ProjectResponse:
        now = datetime.now(UTC).isoformat()
        result = await self._db.update_item(
            table_name=PROJECTS_TABLE,
            key={"teamId": team_id, "sk": f"PROJECT#{project_id}"},
            update_expression="SET #n = :name, updatedAt = :now",
            expression_values={":name": name, ":now": now},
            expression_names={"#n": "name"},
            condition_expression="attribute_exists(sk)",
        )
        return self._to_response(result["Attributes"])

    async def delete_project(self, team_id: str, project_id: str) -> None:
        await self._db.delete_item(
            table_name=PROJECTS_TABLE,
            key={"teamId": team_id, "sk": f"PROJECT#{project_id}"},
        )
        logger.info("project_deleted", project_id=project_id)

    async def advance_stage(self, team_id: str, project_id: str) -> ProjectResponse:
        project = await self._db.get_item(
            table_name=PROJECTS_TABLE,
            key={"teamId": team_id, "sk": f"PROJECT#{project_id}"},
        )
        if not project:
            raise NotFoundException("Project", project_id)

        current = project["currentStage"]
        current_idx = STAGE_ORDER.index(StageType(current))
        if current_idx >= len(STAGE_ORDER) - 1:
            raise ValidationException("Already at final stage")

        stage_status = project["stageStatus"]
        if stage_status.get(current) != StageStatus.COMPLETED:
            raise ValidationException(f"Current stage '{current}' is not completed")

        next_stage = STAGE_ORDER[current_idx + 1]
        now = datetime.now(UTC).isoformat()

        stage_status[next_stage] = StageStatus.IN_PROGRESS

        result = await self._db.update_item(
            table_name=PROJECTS_TABLE,
            key={"teamId": team_id, "sk": f"PROJECT#{project_id}"},
            update_expression="SET currentStage = :ns, stageStatus = :ss, updatedAt = :now",
            expression_values={":ns": next_stage, ":ss": stage_status, ":now": now},
        )
        return self._to_response(result["Attributes"])

    def _to_response(self, item: dict[str, Any]) -> ProjectResponse:
        return ProjectResponse(
            project_id=item["projectId"],
            team_id=item["teamId"],
            name=item["name"],
            current_stage=StageType(item["currentStage"]),
            stage_status=item.get("stageStatus", {}),
            created_at=item["createdAt"],
            updated_at=item["updatedAt"],
            created_by=item["createdBy"],
        )
