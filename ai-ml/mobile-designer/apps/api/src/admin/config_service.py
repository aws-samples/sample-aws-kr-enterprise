from typing import Any

import structlog

from src.common.db.client import DynamoDBClient
from src.common.db.tables import SYSTEM_CONFIG_TABLE

logger = structlog.get_logger()

_PK = "SYSTEM"
_SK = "CONFIG"

_DEFAULT_CONFIG: dict[str, Any] = {
    "registrationOpen": True,
    "maxUsers": 0,
    "maintenanceMode": False,
    "models": {
        "chat": "global.anthropic.claude-sonnet-4-6",
        "wireframe": "global.anthropic.claude-sonnet-4-6",
        "designer": "global.anthropic.claude-sonnet-4-6",
        "modify": "global.anthropic.claude-sonnet-4-6",
        "codegen": "global.anthropic.claude-opus-4-6-v1",
    },
}

_ALLOWED_FIELDS = {"registrationOpen", "maxUsers", "maintenanceMode", "models"}


_cached_models: dict[str, str] | None = None


def get_model_id(slot: str) -> str:
    """Get model ID for a slot from cache. Falls back to defaults if not loaded."""
    default_models: dict[str, str] = _DEFAULT_CONFIG["models"]
    if _cached_models:
        return _cached_models.get(slot, default_models.get(slot, "global.anthropic.claude-sonnet-4-6"))
    return default_models.get(slot, "global.anthropic.claude-sonnet-4-6")


class SystemConfigService:
    def __init__(self, db: DynamoDBClient) -> None:
        self._db = db

    async def get_config(self) -> dict[str, Any]:
        global _cached_models
        item = await self._db.get_item(
            table_name=SYSTEM_CONFIG_TABLE,
            key={"pk": _PK, "sk": _SK},
        )
        if not item:
            _cached_models = _DEFAULT_CONFIG["models"]
            return dict(_DEFAULT_CONFIG)

        models = item.get("models", _DEFAULT_CONFIG["models"])
        _cached_models = models

        return {
            "registrationOpen": item.get("registrationOpen", _DEFAULT_CONFIG["registrationOpen"]),
            "maxUsers": item.get("maxUsers", _DEFAULT_CONFIG["maxUsers"]),
            "maintenanceMode": item.get("maintenanceMode", _DEFAULT_CONFIG["maintenanceMode"]),
            "models": models,
        }

    async def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        filtered = {k: v for k, v in updates.items() if k in _ALLOWED_FIELDS}
        if not filtered:
            return await self.get_config()

        update_parts: list[str] = []
        expr_values: dict[str, Any] = {}
        expr_names: dict[str, str] = {}

        for i, (key, value) in enumerate(filtered.items()):
            alias = f"#f{i}"
            val_alias = f":v{i}"
            update_parts.append(f"{alias} = {val_alias}")
            expr_names[alias] = key
            expr_values[val_alias] = value

        update_expression = "SET " + ", ".join(update_parts)

        # Upsert: create item if it doesn't exist
        existing = await self._db.get_item(
            table_name=SYSTEM_CONFIG_TABLE,
            key={"pk": _PK, "sk": _SK},
        )
        if not existing:
            item = {"pk": _PK, "sk": _SK, **_DEFAULT_CONFIG, **filtered}
            await self._db.put_item(table_name=SYSTEM_CONFIG_TABLE, item=item)
            logger.info("system_config_created", updates=filtered)
            return {k: v for k, v in item.items() if k in _ALLOWED_FIELDS}

        result = await self._db.update_item(
            table_name=SYSTEM_CONFIG_TABLE,
            key={"pk": _PK, "sk": _SK},
            update_expression=update_expression,
            expression_values=expr_values,
            expression_names=expr_names,
        )

        logger.info("system_config_updated", updates=filtered)
        attrs = result.get("Attributes", {})
        return {
            "registrationOpen": attrs.get("registrationOpen", _DEFAULT_CONFIG["registrationOpen"]),
            "maxUsers": attrs.get("maxUsers", _DEFAULT_CONFIG["maxUsers"]),
            "maintenanceMode": attrs.get("maintenanceMode", _DEFAULT_CONFIG["maintenanceMode"]),
            "models": attrs.get("models", _DEFAULT_CONFIG["models"]),
        }
