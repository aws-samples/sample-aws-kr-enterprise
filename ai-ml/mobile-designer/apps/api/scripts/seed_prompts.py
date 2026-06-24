"""Seed initial prompt versions from hardcoded constants in orchestrator and llm_code_generator.

Usage:
    cd apps/api
    python -m scripts.seed_prompts
"""

import asyncio
import sys
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ulid import ULID
from datetime import UTC, datetime

from src.common.config import get_settings
from src.common.db.client import DynamoDBClient
from src.common.db.tables import PROMPTS_TABLE
from src.common.s3.client import S3Client
from src.ai.orchestrator import (
    CHATBOT_SYSTEM_PROMPT,
    WIREFRAME_SYSTEM_PROMPT,
    DESIGNER_SYSTEM_PROMPT,
    MODIFY_SYSTEM_PROMPT,
    WIREFRAME_CHAT_PROMPT,
    DESIGN_CHAT_PROMPT,
    REQUIREMENTS_SYNTHESIS_PROMPT,
)
from src.handoff.code_generator.llm_code_generator import SCREEN_CODEGEN_PROMPT
from src.prompts.slots import (
    CHATBOT_SYSTEM,
    WIREFRAME_SYSTEM,
    DESIGNER_SYSTEM,
    MODIFY_SYSTEM,
    WIREFRAME_CHAT,
    DESIGN_CHAT,
    SCREEN_CODEGEN,
    REQUIREMENTS_SYNTHESIS,
)


SEED_DATA = [
    (CHATBOT_SYSTEM, "Chatbot System Prompt (초기)", CHATBOT_SYSTEM_PROMPT),
    (WIREFRAME_SYSTEM, "Wireframe System Prompt (초기)", WIREFRAME_SYSTEM_PROMPT),
    (DESIGNER_SYSTEM, "Designer System Prompt (초기)", DESIGNER_SYSTEM_PROMPT),
    (MODIFY_SYSTEM, "Modify System Prompt (초기)", MODIFY_SYSTEM_PROMPT),
    (WIREFRAME_CHAT, "Wireframe Chat Prompt (초기)", WIREFRAME_CHAT_PROMPT),
    (DESIGN_CHAT, "Design Chat Prompt (초기)", DESIGN_CHAT_PROMPT),
    (SCREEN_CODEGEN, "Screen Codegen Prompt (초기)", SCREEN_CODEGEN_PROMPT),
    (REQUIREMENTS_SYNTHESIS, "Requirements Synthesis Prompt (초기)", REQUIREMENTS_SYNTHESIS_PROMPT),
]


async def seed() -> None:
    settings = get_settings()
    db = DynamoDBClient(settings)
    s3 = S3Client(settings)
    now = datetime.now(UTC).isoformat()

    for slot, title, content in SEED_DATA:
        # Check if slot already has versions
        result = await db.query(
            table_name=PROMPTS_TABLE,
            key_condition_expression="promptSlot = :slot",
            expression_values={":slot": slot},
            limit=1,
        )
        if result.get("Items"):
            print(f"  SKIP  {slot} (already has versions)")
            continue

        version_id = str(ULID())
        content_key = f"prompts/{slot}/{version_id}.txt"

        # Upload to S3
        await s3.put_object(content_key, content.encode("utf-8"), content_type="text/plain")

        # Save metadata to DynamoDB with isActive=True
        item = {
            "promptSlot": slot,
            "version": version_id,
            "isActive": True,
            "title": title,
            "contentKey": content_key,
            "createdBy": "SYSTEM_SEED",
            "createdAt": now,
        }
        await db.put_item(table_name=PROMPTS_TABLE, item=item)
        print(f"  SEED  {slot} -> version={version_id}")

    print("\nDone. All prompt slots seeded.")


if __name__ == "__main__":
    asyncio.run(seed())
