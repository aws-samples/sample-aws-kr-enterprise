from datetime import UTC, datetime
from typing import Any

import structlog
from ulid import ULID

from src.common.db.client import DynamoDBClient
from src.common.db.tables import PROJECTS_TABLE, VERSIONS_TABLE
from src.common.exceptions import NotFoundException
from src.common.s3.client import S3Client
from src.projects.models import VersionAction, VersionResponse

logger = structlog.get_logger()


class VersionService:
    def __init__(self, db: DynamoDBClient, s3: S3Client) -> None:
        self._db = db
        self._s3 = s3

    async def create_version(
        self,
        project_id: str,
        team_id: str,
        stage_id: str,
        action: VersionAction,
        command: str,
        snapshot_data: bytes,
        user_id: str,
        parent_version_id: str | None = None,
    ) -> VersionResponse:
        version_id = str(ULID())
        now = datetime.now(UTC).isoformat()

        snapshot_key = f"projects/{project_id}/versions/{version_id}/snapshot.json"
        await self._s3.put_object(snapshot_key, snapshot_data, "application/json")

        item = {
            "projectId": project_id,
            "versionId": version_id,
            "stageVersionPK": f"{project_id}#{stage_id}",
            "stageId": stage_id,
            "action": action,
            "command": command,
            "snapshotKey": snapshot_key,
            "parentVersionId": parent_version_id,
            "createdAt": now,
            "createdBy": user_id,
        }
        await self._db.put_item(table_name=VERSIONS_TABLE, item=item)

        await self._db.update_item(
            table_name=PROJECTS_TABLE,
            key={"teamId": team_id, "sk": f"PROJECT#{project_id}"},
            update_expression="SET latestVersionIds.#stage = :vid, updatedAt = :now",
            expression_values={":vid": version_id, ":now": now},
            expression_names={"#stage": stage_id},
        )

        logger.info("version_created", version_id=version_id, project_id=project_id, action=action)
        return self._to_response(item)

    async def get_version(self, project_id: str, version_id: str) -> VersionResponse:
        item = await self._db.get_item(
            table_name=VERSIONS_TABLE,
            key={"projectId": project_id, "versionId": version_id},
        )
        if not item:
            raise NotFoundException("Version", version_id)
        return self._to_response(item)

    async def list_versions(
        self, project_id: str, stage_id: str | None = None, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        if stage_id:
            kwargs: dict[str, Any] = {
                "table_name": VERSIONS_TABLE,
                "key_condition_expression": "stageVersionPK = :pk",
                "expression_values": {":pk": f"{project_id}#{stage_id}"},
                "index_name": "GSI-StageVersions",
                "scan_forward": False,
                "limit": limit,
            }
        else:
            kwargs = {
                "table_name": VERSIONS_TABLE,
                "key_condition_expression": "projectId = :pid",
                "expression_values": {":pid": project_id},
                "scan_forward": False,
                "limit": limit,
            }

        if cursor:
            kwargs["exclusive_start_key"] = {"projectId": project_id, "versionId": cursor}

        result = await self._db.query(**kwargs)
        items = [self._to_response(i) for i in result.get("Items", [])]
        last_key = result.get("LastEvaluatedKey")

        return {
            "items": items,
            "next_cursor": last_key["versionId"] if last_key else None,
        }

    async def revert_to_version(
        self, project_id: str, team_id: str, target_version_id: str, user_id: str
    ) -> VersionResponse:
        target = await self._db.get_item(
            table_name=VERSIONS_TABLE,
            key={"projectId": project_id, "versionId": target_version_id},
        )
        if not target:
            raise NotFoundException("Version", target_version_id)

        snapshot_data = await self._s3.get_object(target["snapshotKey"])

        latest_result = await self._db.query(
            table_name=VERSIONS_TABLE,
            key_condition_expression="stageVersionPK = :pk",
            expression_values={":pk": f"{project_id}#{target['stageId']}"},
            index_name="GSI-StageVersions",
            scan_forward=False,
            limit=1,
        )
        latest_items = latest_result.get("Items", [])
        parent_id = latest_items[0]["versionId"] if latest_items else None

        return await self.create_version(
            project_id=project_id,
            team_id=team_id,
            stage_id=target["stageId"],
            action=VersionAction.REVERT,
            command=f"Revert to {target_version_id}",
            snapshot_data=snapshot_data,
            user_id=user_id,
            parent_version_id=parent_id,
        )

    async def get_snapshot(self, project_id: str, version_id: str) -> bytes:
        await self.get_version(project_id, version_id)
        item = await self._db.get_item(
            table_name=VERSIONS_TABLE,
            key={"projectId": project_id, "versionId": version_id},
        )
        if not item:
            raise NotFoundException("Version", version_id)
        return await self._s3.get_object(item["snapshotKey"])

    def _to_response(self, item: dict[str, Any]) -> VersionResponse:
        return VersionResponse(
            version_id=item["versionId"],
            project_id=item["projectId"],
            stage_id=item["stageId"],
            action=item["action"],
            command=item["command"],
            parent_version_id=item.get("parentVersionId"),
            created_at=item["createdAt"],
            created_by=item["createdBy"],
        )
