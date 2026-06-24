from typing import Any, cast

import aioboto3
import structlog
from botocore.exceptions import ClientError

from src.common.config import Settings

logger = structlog.get_logger()


class DynamoDBClient:
    def __init__(self, settings: Settings) -> None:
        self._session = aioboto3.Session()
        self._settings = settings
        self._endpoint_url = settings.dynamodb_endpoint_url
        self._region = settings.aws_region

    def _resource_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"region_name": self._region}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        return kwargs

    async def put_item(self, table_name: str, item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        async with self._session.resource("dynamodb", **self._resource_kwargs()) as dynamodb:
            table = await dynamodb.Table(table_name)
            return cast(dict[str, Any], await table.put_item(Item=item, **kwargs))

    async def get_item(self, table_name: str, key: dict[str, Any]) -> dict[str, Any] | None:
        async with self._session.resource("dynamodb", **self._resource_kwargs()) as dynamodb:
            table = await dynamodb.Table(table_name)
            response = await table.get_item(Key=key)
            return cast(dict[str, Any] | None, response.get("Item"))

    async def update_item(
        self,
        table_name: str,
        key: dict[str, Any],
        update_expression: str,
        expression_values: dict[str, Any] | None = None,
        expression_names: dict[str, str] | None = None,
        condition_expression: str | None = None,
    ) -> dict[str, Any]:
        async with self._session.resource("dynamodb", **self._resource_kwargs()) as dynamodb:
            table = await dynamodb.Table(table_name)
            kwargs: dict[str, Any] = {
                "Key": key,
                "UpdateExpression": update_expression,
                "ReturnValues": "ALL_NEW",
            }
            if expression_values:
                kwargs["ExpressionAttributeValues"] = expression_values
            if expression_names:
                kwargs["ExpressionAttributeNames"] = expression_names
            if condition_expression:
                kwargs["ConditionExpression"] = condition_expression
            return cast(dict[str, Any], await table.update_item(**kwargs))

    async def delete_item(self, table_name: str, key: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        async with self._session.resource("dynamodb", **self._resource_kwargs()) as dynamodb:
            table = await dynamodb.Table(table_name)
            return cast(dict[str, Any], await table.delete_item(Key=key, **kwargs))

    async def query(
        self,
        table_name: str,
        key_condition_expression: str,
        expression_values: dict[str, Any],
        expression_names: dict[str, str] | None = None,
        index_name: str | None = None,
        scan_forward: bool = True,
        limit: int | None = None,
        exclusive_start_key: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._session.resource("dynamodb", **self._resource_kwargs()) as dynamodb:
            table = await dynamodb.Table(table_name)
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": key_condition_expression,
                "ExpressionAttributeValues": expression_values,
                "ScanIndexForward": scan_forward,
            }
            if expression_names:
                kwargs["ExpressionAttributeNames"] = expression_names
            if index_name:
                kwargs["IndexName"] = index_name
            if limit:
                kwargs["Limit"] = limit
            if exclusive_start_key:
                kwargs["ExclusiveStartKey"] = exclusive_start_key
            return cast(dict[str, Any], await table.query(**kwargs))

    async def batch_write(self, table_name: str, items: list[dict[str, Any]]) -> None:
        async with self._session.resource("dynamodb", **self._resource_kwargs()) as dynamodb:
            table = await dynamodb.Table(table_name)
            async with table.batch_writer() as batch:
                for item in items:
                    await batch.put_item(Item=item)

    async def transact_write(self, transact_items: list[dict[str, Any]]) -> None:
        async with self._session.client("dynamodb", **self._resource_kwargs()) as client:
            await client.transact_write_items(TransactItems=transact_items)

    async def scan(self, table_name: str, **kwargs: Any) -> dict[str, Any]:
        async with self._session.resource("dynamodb", **self._resource_kwargs()) as dynamodb:
            table = await dynamodb.Table(table_name)
            return cast(dict[str, Any], await table.scan(**kwargs))

    async def check_table_exists(self, table_name: str) -> bool:
        try:
            async with self._session.client("dynamodb", **self._resource_kwargs()) as client:
                await client.describe_table(TableName=table_name)
                return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return False
            raise
