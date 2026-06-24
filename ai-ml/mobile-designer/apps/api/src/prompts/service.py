from datetime import UTC, datetime

import structlog
from ulid import ULID

from src.common.db.client import DynamoDBClient
from src.common.db.tables import PROMPTS_TABLE
from src.common.exceptions import NotFoundException
from src.common.s3.client import S3Client
from src.prompts.models import PromptSlotSummary, PromptVersion
from src.prompts.slots import ALL_SLOTS

logger = structlog.get_logger()


class PromptService:
    def __init__(self, db: DynamoDBClient, s3: S3Client) -> None:
        self._db = db
        self._s3 = s3

    async def list_slots(self) -> list[PromptSlotSummary]:
        summaries: list[PromptSlotSummary] = []
        for slot in ALL_SLOTS:
            result = await self._db.query(
                table_name=PROMPTS_TABLE,
                key_condition_expression="promptSlot = :slot",
                expression_values={":slot": slot},
            )
            items = result.get("Items", [])
            active_version: str | None = None
            for item in items:
                if item.get("isActive"):
                    active_version = item["version"]
                    break
            summaries.append(PromptSlotSummary(
                slot=slot,
                active_version=active_version,
                total_versions=len(items),
            ))
        return summaries

    async def list_versions(self, slot: str) -> list[PromptVersion]:
        result = await self._db.query(
            table_name=PROMPTS_TABLE,
            key_condition_expression="promptSlot = :slot",
            expression_values={":slot": slot},
            scan_forward=False,
        )
        items = result.get("Items", [])
        return [
            PromptVersion(
                prompt_slot=item["promptSlot"],
                version=item["version"],
                is_active=item.get("isActive", False),
                title=item.get("title", ""),
                content_key=item.get("contentKey", ""),
                created_by=item.get("createdBy", ""),
                created_at=item.get("createdAt", ""),
            )
            for item in items
        ]

    async def create_version(
        self, slot: str, title: str, content: str, user_id: str
    ) -> PromptVersion:
        version_id = str(ULID())
        content_key = f"prompts/{slot}/{version_id}.txt"
        now = datetime.now(UTC).isoformat()

        # Upload content to S3
        await self._s3.put_object(content_key, content.encode("utf-8"), content_type="text/plain")

        # Save metadata to DynamoDB
        item = {
            "promptSlot": slot,
            "version": version_id,
            "isActive": False,
            "title": title,
            "contentKey": content_key,
            "createdBy": user_id,
            "createdAt": now,
        }
        await self._db.put_item(table_name=PROMPTS_TABLE, item=item)

        logger.info("prompt_version_created", slot=slot, version=version_id, user_id=user_id)

        return PromptVersion(
            prompt_slot=slot,
            version=version_id,
            is_active=False,
            title=title,
            content_key=content_key,
            created_by=user_id,
            created_at=now,
        )

    async def activate_version(self, slot: str, version: str) -> None:
        # Verify target version exists
        target = await self._db.get_item(
            table_name=PROMPTS_TABLE,
            key={"promptSlot": slot, "version": version},
        )
        if not target:
            raise NotFoundException("PromptVersion", f"{slot}/{version}")

        # Deactivate all other versions in the same slot
        result = await self._db.query(
            table_name=PROMPTS_TABLE,
            key_condition_expression="promptSlot = :slot",
            expression_values={":slot": slot},
        )
        for item in result.get("Items", []):
            if item.get("isActive") and item["version"] != version:
                await self._db.update_item(
                    table_name=PROMPTS_TABLE,
                    key={"promptSlot": slot, "version": item["version"]},
                    update_expression="SET isActive = :val",
                    expression_values={":val": False},
                )

        # Activate target version
        await self._db.update_item(
            table_name=PROMPTS_TABLE,
            key={"promptSlot": slot, "version": version},
            update_expression="SET isActive = :val",
            expression_values={":val": True},
        )

        logger.info("prompt_version_activated", slot=slot, version=version)

    async def get_content(self, slot: str, version: str) -> str:
        item = await self._db.get_item(
            table_name=PROMPTS_TABLE,
            key={"promptSlot": slot, "version": version},
        )
        if not item:
            raise NotFoundException("PromptVersion", f"{slot}/{version}")

        content_key = item["contentKey"]
        data = await self._s3.get_object(content_key)
        return data.decode("utf-8")
