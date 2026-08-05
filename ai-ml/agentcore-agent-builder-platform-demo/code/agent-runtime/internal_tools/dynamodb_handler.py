"""DynamoDB internalTools handlers (query/get/put). Spec Section 5.5."""

import json
import os
from typing import Any, Callable


def create_dynamodb_query(tool_config: dict, dynamodb_resource: Any) -> Callable:
    table_name = tool_config.get(
        "table", os.environ.get("DYNAMODB_TABLE", "aiops-v2-dev-platform")
    )
    name = tool_config["name"]
    description = tool_config.get("description", "")
    index_name = tool_config.get("index")
    fixed_pk = tool_config.get("fixedPK")

    def tool_fn(entity_type: str = "", pk_value: str = "", limit: int = 50) -> str:
        """DynamoDB query. GSI index 설정 시 entityType 기반 조회, 아니면 PK 기반 조회."""
        from boto3.dynamodb.conditions import Key

        table = dynamodb_resource.Table(table_name)

        # GSI query (entityType-index)
        if index_name:
            query_key = fixed_pk or entity_type
            if not query_key:
                return json.dumps({"error": "entity_type is required for GSI query"})
            key_condition = Key("entityType").eq(query_key)
            response = table.query(
                IndexName=index_name,
                KeyConditionExpression=key_condition,
                Limit=limit,
            )
            return json.dumps(response.get("Items", []), default=str)

        # Standard PK query. Honor fixedPK so config-pinned queries (e.g. the
        # supervisor's load_agent_registry over PK=SUPERVISOR) work without the
        # LLM having to supply pk_value.
        query_pk = fixed_pk or pk_value
        if not query_pk:
            return json.dumps({"error": "pk_value is required"})
        key_condition = Key("PK").eq(query_pk)
        response = table.query(KeyConditionExpression=key_condition, Limit=limit)
        return json.dumps(response.get("Items", []), default=str)

    tool_fn.__name__ = name
    tool_fn.__doc__ = description
    return tool_fn


def create_dynamodb_get(tool_config: dict, dynamodb_resource: Any) -> Callable:
    table_name = tool_config.get(
        "table", os.environ.get("DYNAMODB_TABLE", "aiops-v2-dev-platform")
    )
    name = tool_config["name"]
    description = tool_config.get("description", "")

    def tool_fn(pk: str, sk: str) -> str:
        table = dynamodb_resource.Table(table_name)
        response = table.get_item(Key={"PK": pk, "SK": sk})
        item = response.get("Item")
        return (
            json.dumps(item, default=str)
            if item
            else json.dumps({"error": "Item not found"})
        )

    tool_fn.__name__ = name
    tool_fn.__doc__ = description
    return tool_fn


def create_dynamodb_put(tool_config: dict, dynamodb_resource: Any) -> Callable:
    table_name = tool_config.get(
        "table", os.environ.get("DYNAMODB_TABLE", "aiops-v2-dev-platform")
    )
    name = tool_config["name"]
    description = tool_config.get("description", "")

    def tool_fn(item: dict) -> str:
        table = dynamodb_resource.Table(table_name)
        table.put_item(Item=item)
        return json.dumps({"status": "created", "table": table_name})

    tool_fn.__name__ = name
    tool_fn.__doc__ = description
    return tool_fn
