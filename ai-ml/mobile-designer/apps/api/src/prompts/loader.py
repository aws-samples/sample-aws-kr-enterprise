import structlog

from src.common.db.client import DynamoDBClient
from src.common.db.tables import PROMPTS_TABLE
from src.common.s3.client import S3Client

logger = structlog.get_logger()


class PromptLoader:
    def __init__(self, db: DynamoDBClient, s3: S3Client) -> None:
        self._db = db
        self._s3 = s3

    async def get(self, slot: str) -> str | None:
        """Read active prompt directly from DB + S3 on every call."""
        try:
            result = await self._db.query(
                table_name=PROMPTS_TABLE,
                key_condition_expression="promptSlot = :slot",
                expression_values={":slot": slot},
            )
            active_item = None
            for item in result.get("Items", []):
                if item.get("isActive"):
                    active_item = item
                    break

            if not active_item:
                return None

            content_key = active_item["contentKey"]
            data = await self._s3.get_object(content_key)
            return data.decode("utf-8")
        except Exception:
            # Loading failed (DB/S3/permission error). Callers fall back to the
            # hardcoded default prompt, so surface this at error level — a silent
            # fallback otherwise masks prompt misconfiguration in production.
            logger.error("prompt_loader_failed", slot=slot, exc_info=True)
            return None


# Module-level singleton
_instance: PromptLoader | None = None


def get_prompt_loader() -> PromptLoader | None:
    return _instance


def set_prompt_loader(loader: PromptLoader) -> None:
    global _instance
    _instance = loader
