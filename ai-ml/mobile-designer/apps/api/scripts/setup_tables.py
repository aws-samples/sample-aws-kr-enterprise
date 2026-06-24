"""DynamoDB table creation script for local development."""
import asyncio
import sys

import aioboto3


TABLE_DEFINITIONS = {
    "MDesigner-Users": {
        "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "userId", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "GSI-Email",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
    "MDesigner-Teams": {
        "KeySchema": [
            {"AttributeName": "teamId", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "teamId", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "userId", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "GSI-UserTeams",
                "KeySchema": [
                    {"AttributeName": "userId", "KeyType": "HASH"},
                    {"AttributeName": "teamId", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
    "MDesigner-Projects": {
        "KeySchema": [
            {"AttributeName": "teamId", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "teamId", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
    },
    "MDesigner-Versions": {
        "KeySchema": [
            {"AttributeName": "projectId", "KeyType": "HASH"},
            {"AttributeName": "versionId", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "projectId", "AttributeType": "S"},
            {"AttributeName": "versionId", "AttributeType": "S"},
            {"AttributeName": "stageVersionPK", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "GSI-StageVersions",
                "KeySchema": [
                    {"AttributeName": "stageVersionPK", "KeyType": "HASH"},
                    {"AttributeName": "versionId", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
    "MDesigner-Files": {
        "KeySchema": [
            {"AttributeName": "projectId", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "projectId", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
    },
    "MDesigner-Comments": {
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "commentId", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "commentId", "AttributeType": "S"},
        ],
    },
    "MDesigner-ShareLinks": {
        "KeySchema": [{"AttributeName": "shareToken", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "shareToken", "AttributeType": "S"},
        ],
    },
    "MDesigner-RefreshTokens": {
        "KeySchema": [
            {"AttributeName": "userId", "KeyType": "HASH"},
            {"AttributeName": "tokenId", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "userId", "AttributeType": "S"},
            {"AttributeName": "tokenId", "AttributeType": "S"},
        ],
    },
    "MDesigner-AITasks": {
        "KeySchema": [{"AttributeName": "taskId", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "taskId", "AttributeType": "S"},
            {"AttributeName": "projectStage", "AttributeType": "S"},
            {"AttributeName": "createdAt", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": "GSI-ProjectStage",
                "KeySchema": [
                    {"AttributeName": "projectStage", "KeyType": "HASH"},
                    {"AttributeName": "createdAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
}


async def create_tables(endpoint_url: str = "http://localhost:8000") -> None:
    session = aioboto3.Session()
    async with session.client("dynamodb", region_name="us-east-1", endpoint_url=endpoint_url) as client:
        existing = await client.list_tables()
        existing_names = existing.get("TableNames", [])

        for table_name, definition in TABLE_DEFINITIONS.items():
            if table_name in existing_names:
                print(f"  [skip] {table_name} already exists")
                continue

            kwargs = {
                "TableName": table_name,
                "KeySchema": definition["KeySchema"],
                "AttributeDefinitions": definition["AttributeDefinitions"],
                "BillingMode": "PAY_PER_REQUEST",
            }
            if "GlobalSecondaryIndexes" in definition:
                gsis = []
                for gsi in definition["GlobalSecondaryIndexes"]:
                    gsis.append({**gsi, "Projection": gsi.get("Projection", {"ProjectionType": "ALL"})})
                kwargs["GlobalSecondaryIndexes"] = gsis

            await client.create_table(**kwargs)
            print(f"  [created] {table_name}")

    print("\nAll tables ready.")


if __name__ == "__main__":
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    print(f"Creating DynamoDB tables at {endpoint}...")
    asyncio.run(create_tables(endpoint))
