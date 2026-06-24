import os

_TABLE_PREFIX = os.environ.get("MDESIGNER_TABLE_PREFIX", "MDesigner")

USERS_TABLE = f"{_TABLE_PREFIX}-Users"
TEAMS_TABLE = f"{_TABLE_PREFIX}-Teams"
PROJECTS_TABLE = f"{_TABLE_PREFIX}-Projects"
VERSIONS_TABLE = f"{_TABLE_PREFIX}-Versions"
FILES_TABLE = f"{_TABLE_PREFIX}-Files"
COMMENTS_TABLE = f"{_TABLE_PREFIX}-Comments"
SHARE_LINKS_TABLE = f"{_TABLE_PREFIX}-ShareLinks"
REFRESH_TOKENS_TABLE = f"{_TABLE_PREFIX}-RefreshTokens"
PROMPTS_TABLE = f"{_TABLE_PREFIX}-Prompts"
SYSTEM_CONFIG_TABLE = f"{_TABLE_PREFIX}-SystemConfig"
AI_TASKS_TABLE = f"{_TABLE_PREFIX}-AITasks"

TABLE_DEFINITIONS = {
    USERS_TABLE: {
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
    TEAMS_TABLE: {
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
    PROJECTS_TABLE: {
        "KeySchema": [
            {"AttributeName": "teamId", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "teamId", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
    },
    VERSIONS_TABLE: {
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
    FILES_TABLE: {
        "KeySchema": [
            {"AttributeName": "projectId", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "projectId", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
    },
    COMMENTS_TABLE: {
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "commentId", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "commentId", "AttributeType": "S"},
        ],
    },
    SHARE_LINKS_TABLE: {
        "KeySchema": [{"AttributeName": "shareToken", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "shareToken", "AttributeType": "S"},
        ],
    },
    REFRESH_TOKENS_TABLE: {
        "KeySchema": [
            {"AttributeName": "userId", "KeyType": "HASH"},
            {"AttributeName": "tokenId", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "userId", "AttributeType": "S"},
            {"AttributeName": "tokenId", "AttributeType": "S"},
        ],
    },
    PROMPTS_TABLE: {
        "KeySchema": [
            {"AttributeName": "promptSlot", "KeyType": "HASH"},
            {"AttributeName": "version", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "promptSlot", "AttributeType": "S"},
            {"AttributeName": "version", "AttributeType": "S"},
        ],
    },
    SYSTEM_CONFIG_TABLE: {
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
    },
    # AI generate/modify/handoff task state shared across API instances.
    # PK = taskId. GSI-ProjectStage resolves the latest task for a project+stage.
    AI_TASKS_TABLE: {
        "KeySchema": [
            {"AttributeName": "taskId", "KeyType": "HASH"},
        ],
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
