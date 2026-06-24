"""
MDesigner Bootstrap Lambda Handler.

CloudFormation Custom Resource that:
1. Creates admin user with bcrypt-hashed password (mustChangePassword=true)
2. Creates admin's personal team
3. Seeds default prompt slots (content stored in S3)
4. Creates SystemConfig initial item

On Update/Delete: no-op (returns SUCCESS).
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import bcrypt
import boto3
import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

http = urllib3.PoolManager()

USERS_TABLE = os.environ["USERS_TABLE"]
TEAMS_TABLE = os.environ["TEAMS_TABLE"]
PROMPTS_TABLE = os.environ["PROMPTS_TABLE"]
SYSTEM_CONFIG_TABLE = os.environ["SYSTEM_CONFIG_TABLE"]
S3_BUCKET = os.environ["S3_BUCKET"]
TABLE_PREFIX = os.environ.get("TABLE_PREFIX", "MDesigner")

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

PROMPT_SLOTS = [
    "CHATBOT_SYSTEM",
    "WIREFRAME_SYSTEM",
    "DESIGNER_SYSTEM",
    "MODIFY_SYSTEM",
    "WIREFRAME_CHAT",
    "DESIGN_CHAT",
    "SCREEN_CODEGEN",
    "REQUIREMENTS_SYNTHESIS",
]

DEFAULT_PROMPT_CONTENT = {
    "CHATBOT_SYSTEM": "You are a helpful mobile UI design assistant.",
    "WIREFRAME_SYSTEM": "You are a wireframe generation specialist for mobile UI.",
    "DESIGNER_SYSTEM": "You are a mobile UI designer specializing in modern mobile design systems.",
    "MODIFY_SYSTEM": "You are a mobile UI modification specialist.",
    "WIREFRAME_CHAT": "Generate a wireframe based on user requirements.",
    "DESIGN_CHAT": "Generate a high-fidelity design based on the wireframe.",
    "SCREEN_CODEGEN": "Generate production-ready code for the given screen design.",
    "REQUIREMENTS_SYNTHESIS": "You are a UI/UX requirements synthesis specialist.",
}


def generate_ulid() -> str:
    """Generate a ULID-like ID using timestamp + random."""
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    ts_hex = format(ts, "012x")
    rand_hex = uuid.uuid4().hex[:20]
    return (ts_hex + rand_hex).upper()[:26]


def send_response(
    event: dict[str, Any],
    context: Any,
    status: str,
    reason: str = "",
    data: dict[str, Any] | None = None,
) -> None:
    """Send response to CloudFormation."""
    response_body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": event.get("PhysicalResourceId", context.log_stream_name),
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {"Status": status},
    }

    json_body = json.dumps(response_body).encode("utf-8")
    response_url = event["ResponseURL"]

    logger.info("Sending %s response to CloudFormation", status)

    try:
        http.request(
            "PUT",
            response_url,
            body=json_body,
            headers={"Content-Type": ""},
        )
    except Exception as e:
        logger.error("Failed to send response: %s", str(e))
        raise


def create_admin_user(email: str, password: str) -> tuple[str, str]:
    """Create admin user and personal team. Returns (user_id, team_id)."""
    users_table = dynamodb.Table(USERS_TABLE)
    teams_table = dynamodb.Table(TEAMS_TABLE)

    # Check if admin already exists
    response = users_table.query(
        IndexName="GSI-Email",
        KeyConditionExpression="email = :email",
        ExpressionAttributeValues={":email": email},
        Limit=1,
    )
    if response.get("Items"):
        existing = response["Items"][0]
        logger.info("Admin user '%s' already exists (userId=%s)", email, existing["userId"])
        return existing["userId"], existing.get("personalTeamId", "")

    user_id = generate_ulid()
    team_id = generate_ulid()
    now = datetime.now(timezone.utc).isoformat()

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    user_item = {
        "userId": user_id,
        "email": email,
        "name": "Admin",
        "passwordHash": password_hash,
        "personalTeamId": team_id,
        "role": "admin",
        "mustChangePassword": True,
        "createdAt": now,
        "updatedAt": now,
    }
    users_table.put_item(
        Item=user_item,
        ConditionExpression="attribute_not_exists(userId)",
    )
    logger.info("Created admin user: %s (userId=%s)", email, user_id)

    # Create personal team
    team_meta = {
        "teamId": team_id,
        "sk": "TEAM#meta",
        "name": "Admin's Workspace",
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

    logger.info("Created personal team: %s (teamId=%s)", "Admin's Workspace", team_id)
    return user_id, team_id


def seed_prompts() -> int:
    """Seed default prompt slots with initial content in S3. Returns count seeded."""
    prompts_table = dynamodb.Table(PROMPTS_TABLE)
    now = datetime.now(timezone.utc).isoformat()
    seeded = 0

    for slot in PROMPT_SLOTS:
        # Check if slot already has versions
        response = prompts_table.query(
            KeyConditionExpression="promptSlot = :slot",
            ExpressionAttributeValues={":slot": slot},
            Limit=1,
        )
        if response.get("Items"):
            logger.info("Prompt slot '%s' already seeded, skipping", slot)
            continue

        version_id = generate_ulid()
        content_key = f"prompts/{slot}/{version_id}.txt"
        content = DEFAULT_PROMPT_CONTENT.get(slot, f"Default prompt for {slot}")

        # Upload content to S3
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=content_key,
            Body=content.encode("utf-8"),
            ContentType="text/plain",
        )

        # Save metadata to DynamoDB
        item = {
            "promptSlot": slot,
            "version": version_id,
            "isActive": True,
            "title": f"{slot} (Initial)",
            "contentKey": content_key,
            "createdBy": "SYSTEM_BOOTSTRAP",
            "createdAt": now,
        }
        prompts_table.put_item(Item=item)
        logger.info("Seeded prompt slot: %s (version=%s)", slot, version_id)
        seeded += 1

    return seeded


def create_system_config() -> None:
    """Create initial SystemConfig entry."""
    config_table = dynamodb.Table(SYSTEM_CONFIG_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    # Check if already exists
    response = config_table.get_item(
        Key={"pk": "SYSTEM", "sk": "CONFIG#general"},
    )
    if response.get("Item"):
        logger.info("SystemConfig already exists, skipping")
        return

    item = {
        "pk": "SYSTEM",
        "sk": "CONFIG#general",
        "maxFileSizeMb": 20,
        "allowedFileTypes": ["pdf", "docx", "md", "txt", "png", "jpg", "webp"],
        "maintenanceMode": False,
        "createdAt": now,
        "updatedAt": now,
    }
    config_table.put_item(Item=item)
    logger.info("Created initial SystemConfig")


def handler(event: dict[str, Any], context: Any) -> None:
    """CloudFormation Custom Resource handler."""
    logger.info("Received event: %s", json.dumps(event, default=str))

    request_type = event.get("RequestType", "")
    properties = event.get("ResourceProperties", {})

    # Only perform actions on Create
    if request_type != "Create":
        logger.info("Request type is '%s', returning success (no-op)", request_type)
        send_response(event, context, "SUCCESS", data={"Status": "NO_OP"})
        return

    try:
        admin_email = properties.get("AdminEmail", "admin@mdesigner.dev")
        admin_password = properties.get("AdminPassword", "")

        if not admin_password:
            raise ValueError("AdminPassword is required")

        # Step 1: Create admin user and team
        user_id, team_id = create_admin_user(admin_email, admin_password)

        # Step 2: Seed prompts
        seeded_count = seed_prompts()

        # Step 3: Create system config
        create_system_config()

        data = {
            "Status": "COMPLETED",
            "AdminUserId": user_id,
            "AdminTeamId": team_id,
            "PromptsSeeded": str(seeded_count),
        }
        logger.info("Bootstrap completed successfully: %s", json.dumps(data))
        send_response(event, context, "SUCCESS", data=data)

    except Exception as e:
        logger.error("Bootstrap failed: %s", str(e), exc_info=True)
        send_response(event, context, "FAILED", reason=str(e))
