import os
import re
from collections.abc import AsyncGenerator
from typing import Any

import boto3
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

os.environ["MDESIGNER_ENVIRONMENT"] = "test"
os.environ["MDESIGNER_AWS_REGION"] = "us-east-1"
os.environ["MDESIGNER_S3_BUCKET_NAME"] = "test-bucket"
os.environ["MDESIGNER_JWT_SECRET_NAME"] = "integration-test-secret-key-32bytes!"
os.environ["MDESIGNER_CORS_ORIGINS"] = '["http://localhost:5173"]'
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

TABLE_DEFINITIONS = {
    "MDesigner-Users": {
        "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": "userId", "AttributeType": "S"}, {"AttributeName": "email", "AttributeType": "S"}],
        "GlobalSecondaryIndexes": [{"IndexName": "GSI-Email", "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}], "Projection": {"ProjectionType": "ALL"}}],
    },
    "MDesigner-Teams": {
        "KeySchema": [{"AttributeName": "teamId", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        "AttributeDefinitions": [{"AttributeName": "teamId", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}, {"AttributeName": "userId", "AttributeType": "S"}],
        "GlobalSecondaryIndexes": [{"IndexName": "GSI-UserTeams", "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}, {"AttributeName": "teamId", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}}],
    },
    "MDesigner-Projects": {
        "KeySchema": [{"AttributeName": "teamId", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        "AttributeDefinitions": [{"AttributeName": "teamId", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
    },
    "MDesigner-Versions": {
        "KeySchema": [{"AttributeName": "projectId", "KeyType": "HASH"}, {"AttributeName": "versionId", "KeyType": "RANGE"}],
        "AttributeDefinitions": [{"AttributeName": "projectId", "AttributeType": "S"}, {"AttributeName": "versionId", "AttributeType": "S"}, {"AttributeName": "stageVersionPK", "AttributeType": "S"}],
        "GlobalSecondaryIndexes": [{"IndexName": "GSI-StageVersions", "KeySchema": [{"AttributeName": "stageVersionPK", "KeyType": "HASH"}, {"AttributeName": "versionId", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}}],
    },
    "MDesigner-Files": {
        "KeySchema": [{"AttributeName": "projectId", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        "AttributeDefinitions": [{"AttributeName": "projectId", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
    },
    "MDesigner-Comments": {
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "commentId", "KeyType": "RANGE"}],
        "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "commentId", "AttributeType": "S"}],
    },
    "MDesigner-ShareLinks": {
        "KeySchema": [{"AttributeName": "shareToken", "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": "shareToken", "AttributeType": "S"}],
    },
    "MDesigner-RefreshTokens": {
        "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}, {"AttributeName": "tokenId", "KeyType": "RANGE"}],
        "AttributeDefinitions": [{"AttributeName": "userId", "AttributeType": "S"}, {"AttributeName": "tokenId", "AttributeType": "S"}],
    },
}


class SyncDynamoDBClient:
    def __init__(self) -> None:
        self._resource = boto3.resource("dynamodb", region_name="us-east-1")

    async def put_item(self, table_name: str, item: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        table = self._resource.Table(table_name)
        return table.put_item(Item=item, **kwargs)

    async def get_item(self, table_name: str, key: dict[str, Any]) -> dict[str, Any] | None:
        table = self._resource.Table(table_name)
        response = table.get_item(Key=key)
        return response.get("Item")

    async def update_item(self, table_name: str, key: dict[str, Any], update_expression: str,
                          expression_values: dict[str, Any] | None = None,
                          expression_names: dict[str, str] | None = None,
                          condition_expression: str | None = None) -> dict[str, Any]:
        table = self._resource.Table(table_name)
        kwargs: dict[str, Any] = {"Key": key, "UpdateExpression": update_expression, "ReturnValues": "ALL_NEW"}
        if expression_values:
            kwargs["ExpressionAttributeValues"] = expression_values
        if expression_names:
            kwargs["ExpressionAttributeNames"] = expression_names
        if condition_expression:
            kwargs["ConditionExpression"] = condition_expression
        return table.update_item(**kwargs)

    async def delete_item(self, table_name: str, key: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        table = self._resource.Table(table_name)
        return table.delete_item(Key=key, **kwargs)

    async def query(self, table_name: str, key_condition_expression: str, expression_values: dict[str, Any],
                    expression_names: dict[str, str] | None = None, index_name: str | None = None,
                    scan_forward: bool = True, limit: int | None = None,
                    exclusive_start_key: dict[str, Any] | None = None) -> dict[str, Any]:
        from boto3.dynamodb.conditions import Key
        table = self._resource.Table(table_name)

        parts = [p.strip() for p in key_condition_expression.split(" AND ")]
        condition = None
        for part in parts:
            if "begins_with" in part:
                m = re.match(r"begins_with\((\w+),\s*(\S+)\)", part)
                if m:
                    attr_name = m.group(1)
                    val_key = m.group(2)
                    c = Key(attr_name).begins_with(expression_values[val_key])
                else:
                    continue
            elif "=" in part:
                attr, val_key = [x.strip() for x in part.split("=")]
                if attr.startswith("#") and expression_names:
                    attr = expression_names[attr]
                c = Key(attr).eq(expression_values[val_key])
            else:
                continue
            condition = c if condition is None else condition & c

        qkwargs: dict[str, Any] = {"ScanIndexForward": scan_forward}
        if condition:
            qkwargs["KeyConditionExpression"] = condition
        if index_name:
            qkwargs["IndexName"] = index_name
        if limit:
            qkwargs["Limit"] = limit
        if exclusive_start_key:
            qkwargs["ExclusiveStartKey"] = exclusive_start_key
        return table.query(**qkwargs)

    async def batch_write(self, table_name: str, items: list[dict[str, Any]]) -> None:
        table = self._resource.Table(table_name)
        with table.batch_writer() as batch:
            for item in items:
                batch.put_item(Item=item)


class SyncS3Client:
    def __init__(self) -> None:
        self._client = boto3.client("s3", region_name="us-east-1")
        self._bucket = "test-bucket"

    async def generate_presigned_upload_url(self, key: str, content_type: str, max_size_bytes: int) -> dict[str, Any]:
        url = self._client.generate_presigned_url("put_object", Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type}, ExpiresIn=3600)
        return {"url": url, "key": key, "fields": {"Content-Type": content_type}, "max_size_bytes": max_size_bytes}

    async def generate_presigned_download_url(self, key: str, filename: str | None = None) -> str:
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        return self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=3600)

    async def put_object(self, key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body, ContentType=content_type)

    async def get_object(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    async def head_object(self, key: str) -> dict[str, Any] | None:
        try:
            return self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception:
            return None


def _create_tables():
    client = boto3.client("dynamodb", region_name="us-east-1")
    for table_name, defn in TABLE_DEFINITIONS.items():
        kwargs = {"TableName": table_name, "KeySchema": defn["KeySchema"], "AttributeDefinitions": defn["AttributeDefinitions"], "BillingMode": "PAY_PER_REQUEST"}
        if "GlobalSecondaryIndexes" in defn:
            kwargs["GlobalSecondaryIndexes"] = defn["GlobalSecondaryIndexes"]
        client.create_table(**kwargs)
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket")


@pytest.fixture(scope="session")
def aws_mock():
    with mock_aws():
        _create_tables()
        yield


@pytest_asyncio.fixture
async def client(aws_mock) -> AsyncGenerator[AsyncClient, None]:
    from src.common.config import get_settings
    from src.common.dependencies import get_db, get_s3
    get_settings.cache_clear()
    get_db.cache_clear()
    get_s3.cache_clear()

    db_client = SyncDynamoDBClient()
    s3_client = SyncS3Client()

    from src.main import create_app
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_client
    app.dependency_overrides[get_s3] = lambda: s3_client

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient) -> dict[str, str]:
    await client.post("/auth/register", json={"email": "inttest@example.com", "name": "Integration Tester", "password": "TestPass123"})
    resp = await client.post("/auth/login", json={"email": "inttest@example.com", "password": "TestPass123"})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
