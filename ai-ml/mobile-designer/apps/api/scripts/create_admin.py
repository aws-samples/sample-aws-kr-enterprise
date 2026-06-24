"""Standalone script to create an admin user directly in DynamoDB."""

import os
import sys
from datetime import UTC, datetime

import bcrypt
import boto3
from ulid import ULID

TABLE_PREFIX = os.environ.get("MDESIGNER_TABLE_PREFIX", "MDesigner")
USERS_TABLE = f"{TABLE_PREFIX}-Users"
TEAMS_TABLE = f"{TABLE_PREFIX}-Teams"
DYNAMODB_ENDPOINT = os.environ.get("DYNAMODB_ENDPOINT_URL")
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@mdesigner.dev")
ADMIN_NAME = os.environ.get("ADMIN_NAME", "Admin")
# Local dev only. Override via env; do not hardcode a real password.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ChangeMe123!")


def get_dynamodb_resource():
    kwargs = {"region_name": AWS_REGION}
    if DYNAMODB_ENDPOINT:
        kwargs["endpoint_url"] = DYNAMODB_ENDPOINT
    return boto3.resource("dynamodb", **kwargs)


def main() -> None:
    dynamodb = get_dynamodb_resource()
    users_table = dynamodb.Table(USERS_TABLE)
    teams_table = dynamodb.Table(TEAMS_TABLE)

    # Check if admin already exists
    response = users_table.query(
        IndexName="GSI-Email",
        KeyConditionExpression="email = :email",
        ExpressionAttributeValues={":email": ADMIN_EMAIL},
        Limit=1,
    )
    if response.get("Items"):
        print(f"Admin user '{ADMIN_EMAIL}' already exists. Skipping creation.")
        sys.exit(0)

    user_id = str(ULID())
    team_id = str(ULID())
    now = datetime.now(UTC).isoformat()

    password_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()

    user_item = {
        "userId": user_id,
        "email": ADMIN_EMAIL,
        "name": ADMIN_NAME,
        "passwordHash": password_hash,
        "personalTeamId": team_id,
        "role": "admin",
        "mustChangePassword": False,
        "createdAt": now,
        "updatedAt": now,
    }
    users_table.put_item(Item=user_item, ConditionExpression="attribute_not_exists(userId)")

    team_meta = {
        "teamId": team_id,
        "sk": "TEAM#meta",
        "name": f"{ADMIN_NAME}'s Workspace",
        "type": "personal",
        "createdAt": now,
        "createdBy": user_id,
    }
    membership = {
        "teamId": team_id,
        "sk": f"MEMBER#{user_id}",
        "userId": user_id,
        "role": "owner",
        "joinedAt": now,
        "invitedBy": user_id,
    }

    with teams_table.batch_writer() as batch:
        batch.put_item(Item=team_meta)
        batch.put_item(Item=membership)

    print(f"Admin user created successfully:")
    print(f"  Email: {ADMIN_EMAIL}")
    print(f"  Name: {ADMIN_NAME}")
    print(f"  Password: {ADMIN_PASSWORD}")
    print(f"  Role: admin")
    print(f"  User ID: {user_id}")
    print(f"  Team ID: {team_id}")


if __name__ == "__main__":
    main()
